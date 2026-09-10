"""Validazione: backtest ripetibili, walk-forward, sensitivita' e Deflated Sharpe."""

from __future__ import annotations

import inspect
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from itertools import product
from pathlib import Path
from statistics import NormalDist

import pandas as pd

from quant.analysis import compute_metrics
from quant.data import DataHandler, ParquetDataHandler
from quant.engine import Backtest
from quant.execution import SimulatedExecutionHandler
from quant.portfolio import Portfolio
from quant.risk import RiskManager
from quant.strategy import Strategy

PERCORSO_DATI = Path("data/parquet")
GAMMA = 0.5772156649015329  # costante di Eulero-Mascheroni

type StrategyFactory = Callable[..., Strategy]
type DataHandlerFactory = Callable[..., DataHandler]


def build_data_handler(
    path: str | Path,
    symbols: Sequence[str],
    start: str | datetime | None,
    end: str | datetime | None,
) -> DataHandler:
    """Costruttore predefinito del data handler per una finestra temporale."""
    return ParquetDataHandler(path, list(symbols), start=start, end=end)


def run_backtest(
    strategy_factory: StrategyFactory,
    symbols: Sequence[str],
    start: str | datetime | None = None,
    end: str | datetime | None = None,
    path: str | Path = PERCORSO_DATI,
    initial_cash: float = 100_000.0,
    commission_per_trade: float = 1.0,
    slippage_bps: float = 5.0,
    cash_buffer: float = 0.01,
    credit_dividends: bool = True,
    max_weight_per_symbol: float = 1.0,
    max_gross_exposure: float = 1.0,
    max_drawdown: float = 1.0,
    warmup_start: str | datetime | None = None,
    data_handler_factory: DataHandlerFactory = build_data_handler,
) -> dict[str, object]:
    """Monta i cinque componenti su una finestra, esegue e restituisce metriche ed equity curve.

    `strategy_factory` non prende argomenti: il data handler della finestra viene
    collegato dopo la costruzione. Una factory che accetta un argomento riceve invece
    il data handler direttamente.

    Con `warmup_start` il backtest parte prima e le metriche vengono calcolate solo da
    `start`: una strategia con dodici mesi di lookback altrimenti passerebbe il primo
    anno in cassa per mancanza di storico, e il confronto con i benchmark sarebbe falsato.
    """
    data_handler = data_handler_factory(path, symbols, warmup_start or start, end)
    strategy = _costruisci_strategia(strategy_factory, data_handler)
    portfolio = Portfolio(
        data_handler,
        initial_cash=initial_cash,
        cash_buffer=cash_buffer,
        credit_dividends=credit_dividends,
    )
    backtest = Backtest(
        data_handler=data_handler,
        strategy=strategy,
        portfolio=portfolio,
        risk_manager=RiskManager(max_weight_per_symbol, max_gross_exposure, max_drawdown),
        execution_handler=SimulatedExecutionHandler(data_handler, commission_per_trade, slippage_bps),
    )
    backtest.run()

    equity_curve = portfolio.equity_curve
    fills = portfolio.fills
    if warmup_start is not None and start is not None:
        soglia = pd.Timestamp(start)
        equity_curve = [(t, v) for t, v in equity_curve if pd.Timestamp(t) >= soglia]
        fills = [f for f in fills if pd.Timestamp(f.timestamp) >= soglia]
        equity_curve = _riporta_alla_base(equity_curve, initial_cash)

    return {
        "metrics": compute_metrics(equity_curve, fills),
        "equity_curve": equity_curve,
        "fills": fills,
        "portfolio": portfolio,
        "backtest": backtest,
    }


def _costruisci_strategia(strategy_factory: StrategyFactory, data_handler: DataHandler) -> Strategy:
    """Crea la strategia e le collega il data handler della finestra corrente."""
    parametri = inspect.signature(strategy_factory).parameters
    strategy = strategy_factory(data_handler) if parametri else strategy_factory()
    strategy.data_handler = data_handler
    return strategy


def parameter_combinations(param_grid: Mapping[str, Iterable[object]]) -> list[dict[str, object]]:
    """Prodotto cartesiano della griglia, in ordine deterministico."""
    if not param_grid:
        return [{}]
    chiavi = list(param_grid)
    return [dict(zip(chiavi, valori)) for valori in product(*(list(param_grid[k]) for k in chiavi))]


def parameter_sensitivity(
    strategy_factory_from_params: Callable[[dict[str, object]], StrategyFactory],
    param_grid: Mapping[str, Iterable[object]],
    symbols: Sequence[str],
    start: str | datetime | None = None,
    end: str | datetime | None = None,
    **engine_kwargs: object,
) -> pd.DataFrame:
    """Una riga per combinazione di parametri, per distinguere una regione stabile da un picco."""
    righe = []
    for params in parameter_combinations(param_grid):
        risultato = run_backtest(strategy_factory_from_params(params), symbols, start, end, **engine_kwargs)
        righe.append({**params, **risultato["metrics"]})
    return pd.DataFrame(righe)


def walk_forward_windows(
    start: str | datetime,
    end: str | datetime,
    train_years: int = 5,
    test_years: int = 1,
) -> list[tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    """Finestre (train_inizio, train_fine, test_inizio, test_fine) che avanzano di `test_years`."""
    finestre = []
    train_inizio = pd.Timestamp(start)
    limite = pd.Timestamp(end)
    while True:
        train_fine = train_inizio + pd.DateOffset(years=train_years) - pd.Timedelta(days=1)
        test_inizio = train_fine + pd.Timedelta(days=1)
        if test_inizio > limite:
            return finestre
        test_fine = min(test_inizio + pd.DateOffset(years=test_years) - pd.Timedelta(days=1), limite)
        finestre.append((train_inizio, train_fine, test_inizio, test_fine))
        train_inizio = train_inizio + pd.DateOffset(years=test_years)


def walk_forward(
    strategy_factory_from_params: Callable[[dict[str, object]], StrategyFactory],
    param_grid: Mapping[str, Iterable[object]],
    symbols: Sequence[str],
    start: str | datetime,
    end: str | datetime,
    train_years: int = 5,
    test_years: int = 1,
    warmup_years: int = 1,
    **engine_kwargs: object,
) -> pd.DataFrame:
    """Sceglie i parametri per Sharpe sul train e li applica al test successivo.

    Ogni finestra vede solo il proprio periodo piu' il riscaldamento che la precede:
    il data handler viene costruito con `end` alla fine della finestra, quindi le
    barre del test non esistono proprio mentre si scelgono i parametri. L'equity curve
    out-of-sample concatenata finisce in `DataFrame.attrs["equity_oos"]`.
    """
    combinazioni = parameter_combinations(param_grid)
    righe = []
    equity_concatenata: list[tuple[datetime, float]] = []

    for indice, (train_inizio, train_fine, test_inizio, test_fine) in enumerate(
        walk_forward_windows(start, end, train_years, test_years)
    ):
        migliori, metriche_is = _seleziona_parametri(
            strategy_factory_from_params,
            combinazioni,
            symbols,
            train_inizio,
            train_fine,
            warmup_years,
            engine_kwargs,
        )
        risultato_oos = run_backtest(
            strategy_factory_from_params(migliori),
            symbols,
            test_inizio,
            test_fine,
            warmup_start=_riscaldamento(test_inizio, warmup_years),
            **engine_kwargs,
        )
        metriche_oos = risultato_oos["metrics"]

        equity_concatenata = _concatena(equity_concatenata, risultato_oos["equity_curve"])
        righe.append(
            {
                "finestra": indice,
                "train_inizio": train_inizio,
                "train_fine": train_fine,
                "test_inizio": test_inizio,
                "test_fine": test_fine,
                **{f"param_{k}": v for k, v in migliori.items()},
                "sharpe_is": metriche_is["sharpe"],
                "cagr_is": metriche_is["cagr"],
                "max_drawdown_is": metriche_is["max_drawdown"],
                "sharpe_oos": metriche_oos["sharpe"],
                "cagr_oos": metriche_oos["cagr"],
                "max_drawdown_oos": metriche_oos["max_drawdown"],
                "n_trade_oos": metriche_oos["n_trade"],
            }
        )

    tabella = pd.DataFrame(righe)
    tabella.attrs["equity_oos"] = equity_concatenata
    tabella.attrs["n_combinazioni"] = len(combinazioni)
    return tabella


def _seleziona_parametri(
    strategy_factory_from_params: Callable[[dict[str, object]], StrategyFactory],
    combinazioni: list[dict[str, object]],
    symbols: Sequence[str],
    train_inizio: pd.Timestamp,
    train_fine: pd.Timestamp,
    warmup_years: int,
    engine_kwargs: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, float]]:
    """Parametri con lo Sharpe piu' alto sul solo periodo di train."""
    migliori = combinazioni[0]
    metriche_migliori: dict[str, float] | None = None
    for params in combinazioni:
        metriche = run_backtest(
            strategy_factory_from_params(params),
            symbols,
            train_inizio,
            train_fine,
            warmup_start=_riscaldamento(train_inizio, warmup_years),
            **engine_kwargs,
        )["metrics"]
        if metriche_migliori is None or metriche["sharpe"] > metriche_migliori["sharpe"]:
            migliori, metriche_migliori = params, metriche
    assert metriche_migliori is not None
    return migliori, metriche_migliori


def _riporta_alla_base(
    equity_curve: list[tuple[datetime, float]],
    base: float,
) -> list[tuple[datetime, float]]:
    """Riscala l'equity curve perche' la finestra misurata parta dal capitale iniziale.

    Dopo il riscaldamento ogni serie arriva a `start` con un valore diverso: senza
    riscalatura il grafico e l'equity finale confronterebbero capitali diversi. Le
    metriche di rendimento e rischio non cambiano perche' sono invarianti di scala,
    mentre trade e costi restano nella valuta originale del backtest.
    """
    if not equity_curve or equity_curve[0][1] <= 0.0:
        return equity_curve
    fattore = base / equity_curve[0][1]
    return [(t, v * fattore) for t, v in equity_curve]


def _riscaldamento(inizio: pd.Timestamp, warmup_years: int) -> pd.Timestamp | None:
    """Data da cui far partire il backtest perche' la strategia arrivi gia' informata."""
    if warmup_years <= 0:
        return None
    return pd.Timestamp(inizio) - pd.DateOffset(years=warmup_years)


def _concatena(
    accumulata: list[tuple[datetime, float]],
    nuova: Sequence[tuple[datetime, float]],
) -> list[tuple[datetime, float]]:
    """Attacca una equity curve alla precedente riscalandola sull'ultimo valore."""
    if not nuova:
        return accumulata
    base = accumulata[-1][1] if accumulata else nuova[0][1]
    iniziale = nuova[0][1]
    if iniziale <= 0:
        return accumulata
    return accumulata + [(t, base * v / iniziale) for t, v in nuova[1:]]


def expected_max_sharpe(n_trials: int, variance_trials: float = 1.0) -> float:
    """Sharpe massimo atteso fra `n_trials` tentativi indipendenti sotto ipotesi nulla.

    E[max SR] ~ sqrt(V) * [(1 - gamma) * Z^-1(1 - 1/N) + gamma * Z^-1(1 - 1/(N*e))],
    con gamma costante di Eulero-Mascheroni e V varianza degli Sharpe provati.
    """
    n = max(2, int(n_trials))
    normale = NormalDist()
    z1 = normale.inv_cdf(1.0 - 1.0 / n)
    z2 = normale.inv_cdf(1.0 - 1.0 / (n * math.e))
    return math.sqrt(max(variance_trials, 0.0)) * ((1.0 - GAMMA) * z1 + GAMMA * z2)


def deflated_sharpe(
    sharpe_observed: float,
    n_trials: int,
    n_obs: int,
    skew: float,
    kurt: float,
    variance_trials: float | None = None,
) -> float:
    """Deflated Sharpe Ratio di Bailey e Lopez de Prado (2014).

    DSR = Z[ (SR - SR0) * sqrt(T - 1) / sqrt(1 - g1*SR + (g2 - 1)/4 * SR^2) ]

    dove SR e' lo Sharpe osservato NON annualizzato, SR0 lo Sharpe massimo atteso
    fra `n_trials` prove, T il numero di osservazioni, g1 l'asimmetria e g2 la
    curtosi non in eccesso dei rendimenti. Il risultato e' la probabilita' che lo
    Sharpe vero sia positivo una volta scontata la ricerca sui parametri.

    `variance_trials` e' la varianza degli Sharpe provati, nelle stesse unita' di SR.
    Se non viene passata si usa 1/T, cioe' la varianza campionaria dello Sharpe sotto
    ipotesi nulla di rendimenti indipendenti a media zero.
    """
    if n_obs < 2:
        return 0.0
    varianza_prove = 1.0 / n_obs if variance_trials is None else variance_trials
    sr0 = expected_max_sharpe(n_trials, varianza_prove)
    varianza = 1.0 - skew * sharpe_observed + (kurt - 1.0) / 4.0 * sharpe_observed**2
    if varianza <= 0.0:
        return 0.0
    z = (sharpe_observed - sr0) * math.sqrt(n_obs - 1) / math.sqrt(varianza)
    return NormalDist().cdf(z)
