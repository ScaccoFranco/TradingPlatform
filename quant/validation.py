"""Validazione: backtest ripetibili, walk-forward, sensitivita' e Deflated Sharpe."""

from __future__ import annotations

import inspect
import math
import warnings
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from itertools import product
from pathlib import Path
from statistics import NormalDist
from typing import Any, TypedDict

import pandas as pd

from quant.adjust import adjusted_view
from quant.analysis import compute_metrics
from quant.config import BacktestConfig
from quant.data import DataHandler, ParquetDataHandler
from quant.engine import Backtest
from quant.events import FillEvent
from quant.execution import SimulatedExecutionHandler
from quant.logging import get_logger
from quant.portfolio import Portfolio
from quant.provenance import Provenance, build_provenance
from quant.risk import RiskManager
from quant.strategy import Strategy

logger = get_logger("validation")

PERCORSO_DATI = Path("data/parquet")
GAMMA = 0.5772156649015329  # costante di Eulero-Mascheroni
MAX_BARRE = 1_000_000  # tutte le barre visibili: il cursore e' gia' alla fine della finestra

type StrategyFactory = Callable[..., Strategy]
type Params = dict[str, Any]


class BacktestResult(TypedDict):
    """Esito di `run_backtest`: resta un dizionario, ma con un tipo per ogni chiave."""

    metrics: dict[str, float]
    equity_curve: list[tuple[datetime, float]]
    fills: list[FillEvent]
    portfolio: Portfolio
    backtest: Backtest
    config: BacktestConfig
    provenance: Provenance
type DataHandlerFactory = Callable[..., DataHandler]


def build_data_handler(
    path: str | Path,
    symbols: Sequence[str],
    start: str | datetime | None,
    end: str | datetime | None,
) -> DataHandler:
    """Costruttore predefinito del data handler per una finestra temporale."""
    return ParquetDataHandler(path, list(symbols), start=start, end=end)


def build_adjusted_data_handler(
    path: str | Path,
    symbols: Sequence[str],
    start: str | datetime | None,
    end: str | datetime | None,
) -> DataHandler:
    """Data handler sulle barre rettificate, per `BacktestConfig(dividends_as_cash=False)`."""
    return ParquetDataHandler(path, list(symbols), start=start, end=end, transform=adjusted_view)


def run_backtest(
    strategy_factory: StrategyFactory,
    symbols: Sequence[str],
    start: str | datetime | None = None,
    end: str | datetime | None = None,
    config: BacktestConfig | None = None,
    warmup_start: str | datetime | None = None,
    data_handler_factory: DataHandlerFactory = build_data_handler,
    **legacy: Any,
) -> BacktestResult:
    """Monta i cinque componenti su una finestra, esegue e restituisce metriche ed equity curve.

    `strategy_factory` non prende argomenti: il data handler della finestra viene
    collegato dopo la costruzione. Una factory che accetta un argomento riceve invece
    il data handler direttamente.

    Con `warmup_start` il backtest parte prima e le metriche vengono calcolate solo da
    `start`: una strategia con dodici mesi di lookback altrimenti passerebbe il primo
    anno in cassa per mancanza di storico, e il confronto con i benchmark sarebbe falsato.

    I parametri stanno in `BacktestConfig`. I vecchi argomenti sciolti restano accettati
    per una release, con un avviso di deprecazione. Il blocco `provenance` lega il
    risultato a dati, commit e parametri, ed e' raccolto prima di far girare la strategia.
    """
    impostazioni = _config_effettiva(config, legacy)
    if not impostazioni.dividends_as_cash:
        if data_handler_factory is not build_data_handler:
            raise ValueError("dividends_as_cash=False funziona solo con il data handler Parquet predefinito")
        data_handler_factory = build_adjusted_data_handler
    data_handler = data_handler_factory(impostazioni.path, symbols, warmup_start or start, end)
    strategy = _costruisci_strategia(strategy_factory, data_handler)
    provenienza = build_provenance(impostazioni, symbols, start, end, warmup_start, strategy)
    if provenienza.manifest_mismatch:
        logger.warning("dati_fuori_manifest", symbols=list(provenienza.manifest_mismatch))
    portfolio = Portfolio(
        data_handler,
        initial_cash=impostazioni.initial_cash,
        cash_buffer=impostazioni.cash_buffer,
        credit_dividends=impostazioni.dividends_as_cash,
    )
    backtest = Backtest(
        data_handler=data_handler,
        strategy=strategy,
        portfolio=portfolio,
        risk_manager=RiskManager(
            impostazioni.max_weight_per_symbol,
            impostazioni.max_gross_exposure,
            impostazioni.max_drawdown,
        ),
        execution_handler=SimulatedExecutionHandler(
            data_handler, impostazioni.commission_per_trade, impostazioni.slippage_bps
        ),
    )
    backtest.run()

    equity_curve = portfolio.equity_curve
    fills = portfolio.fills
    if warmup_start is not None and start is not None:
        soglia = pd.Timestamp(start)
        equity_curve = [(t, v) for t, v in equity_curve if pd.Timestamp(t) >= soglia]
        fills = [f for f in fills if pd.Timestamp(f.timestamp) >= soglia]
        equity_curve = _riporta_alla_base(equity_curve, impostazioni.initial_cash)

    return {
        "metrics": compute_metrics(
            equity_curve, fills, risk_free=risk_free_series(data_handler, impostazioni.risk_free_symbol)
        ),
        "equity_curve": equity_curve,
        "fills": fills,
        "portfolio": portfolio,
        "backtest": backtest,
        "config": impostazioni,
        "provenance": provenienza,
    }


def _config_effettiva(config: BacktestConfig | None, legacy: Mapping[str, Any]) -> BacktestConfig:
    """Config esplicita, eventualmente sovrascritta dai vecchi argomenti sciolti."""
    base = config or BacktestConfig()
    if not legacy:
        return base
    warnings.warn(
        "gli argomenti sciolti di run_backtest sono deprecati: usare BacktestConfig",
        DeprecationWarning,
        stacklevel=3,
    )
    return base.with_overrides(**dict(legacy))


def risk_free_series(data_handler: DataHandler, symbol: str | None) -> pd.Series | None:
    """Rendimenti giornalieri del titolo monetario, presi dal data handler della finestra.

    Usa `adj_close` perche' e' un rendimento, e serve solo a valutare a posteriori:
    la strategia non lo vede mai.
    """
    if symbol is None or symbol not in getattr(data_handler, "symbols", []):
        return None
    barre = data_handler.get_latest_bars(symbol, MAX_BARRE)
    if barre.empty or "adj_close" not in barre.columns:
        return None
    serie = barre["adj_close"].pct_change().dropna()
    serie.index = pd.DatetimeIndex(serie.index).normalize()
    return serie


def _costruisci_strategia(strategy_factory: StrategyFactory, data_handler: DataHandler) -> Strategy:
    """Crea la strategia e le collega il data handler della finestra corrente."""
    parametri = inspect.signature(strategy_factory).parameters
    strategy = strategy_factory(data_handler) if parametri else strategy_factory()
    strategy.data_handler = data_handler
    return strategy


def parameter_combinations(param_grid: Mapping[str, Iterable[Any]]) -> list[Params]:
    """Prodotto cartesiano della griglia, in ordine deterministico."""
    if not param_grid:
        return [{}]
    chiavi = list(param_grid)
    valori_per_chiave = (list(param_grid[k]) for k in chiavi)
    return [dict(zip(chiavi, valori, strict=True)) for valori in product(*valori_per_chiave)]


def parameter_sensitivity(
    strategy_factory_from_params: Callable[[Params], StrategyFactory],
    param_grid: Mapping[str, Iterable[object]],
    symbols: Sequence[str],
    start: str | datetime | None = None,
    end: str | datetime | None = None,
    config: BacktestConfig | None = None,
    **engine_kwargs: Any,
) -> pd.DataFrame:
    """Una riga per combinazione di parametri, per distinguere una regione stabile da un picco."""
    righe = []
    for params in parameter_combinations(param_grid):
        risultato = run_backtest(
            strategy_factory_from_params(params), symbols, start, end, config=config, **engine_kwargs
        )
        righe.append({**params, **risultato["metrics"], "provenance": risultato["provenance"].hash})
    return pd.DataFrame(righe)


def walk_forward_windows(
    start: str | datetime,
    end: str | datetime,
    train_years: int = 5,
    test_years: int = 1,
) -> list[tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    """Finestre (train_inizio, train_fine, test_inizio, test_fine) che avanzano di `test_years`."""
    finestre: list[tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]] = []
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
    strategy_factory_from_params: Callable[[Params], StrategyFactory],
    param_grid: Mapping[str, Iterable[object]],
    symbols: Sequence[str],
    start: str | datetime,
    end: str | datetime,
    train_years: int = 5,
    test_years: int = 1,
    warmup_years: int = 1,
    config: BacktestConfig | None = None,
    **engine_kwargs: Any,
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
            config,
            engine_kwargs,
        )
        risultato_oos = run_backtest(
            strategy_factory_from_params(migliori),
            symbols,
            test_inizio,
            test_fine,
            config=config,
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
                "provenance_oos": risultato_oos["provenance"].hash,
            }
        )

    tabella = pd.DataFrame(righe)
    tabella.attrs["equity_oos"] = equity_concatenata
    tabella.attrs["n_combinazioni"] = len(combinazioni)
    return tabella


def _seleziona_parametri(
    strategy_factory_from_params: Callable[[Params], StrategyFactory],
    combinazioni: list[Params],
    symbols: Sequence[str],
    train_inizio: pd.Timestamp,
    train_fine: pd.Timestamp,
    warmup_years: int,
    config: BacktestConfig | None,
    engine_kwargs: Mapping[str, Any],
) -> tuple[Params, dict[str, float]]:
    """Parametri con lo Sharpe piu' alto sul solo periodo di train."""
    migliori = combinazioni[0]
    metriche_migliori: dict[str, float] | None = None
    for params in combinazioni:
        metriche = run_backtest(
            strategy_factory_from_params(params),
            symbols,
            train_inizio,
            train_fine,
            config=config,
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
