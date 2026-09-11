"""Test della validazione: finestre, selezione dei parametri, Deflated Sharpe."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from quant.config import BacktestConfig
from quant.data import ParquetDataHandler
from quant.events import MarketEvent, SignalDirection, SignalEvent
from quant.strategy import Strategy
from quant.validation import (
    deflated_sharpe,
    expected_max_sharpe,
    parameter_combinations,
    parameter_sensitivity,
    run_backtest,
    walk_forward,
    walk_forward_windows,
)

SIMBOLI = ["AAA", "CCC", "SPY", "SHY"]
GRIGLIA = {"symbol": ["AAA", "CCC"]}


class CompraSimbolo(Strategy):
    """Strategia minima parametrica: entra sul simbolo scelto alla prima barra."""

    def __init__(self, symbol: str, data_handler=None) -> None:
        super().__init__(data_handler)
        self.symbol = symbol
        self._fatto = False

    def on_bar(self, event: MarketEvent) -> list[SignalEvent]:
        """Un solo ingresso, poi silenzio."""
        assert self.data_handler is not None
        if self._fatto or self.data_handler.get_latest_bars(self.symbol, 1).empty:
            return []
        self._fatto = True
        return [SignalEvent(event.timestamp, self.symbol, SignalDirection.LONG, 1.0)]


def factory_da_parametri(params: dict) -> object:
    """Adatta un dizionario di parametri a una strategy factory senza argomenti."""
    return lambda: CompraSimbolo(params["symbol"])


class HandlerSpia(ParquetDataHandler):
    """Data handler che registra ogni data effettivamente restituita."""

    def __init__(self, path, symbols, start, end, registro: list) -> None:
        super().__init__(path, symbols, start=start, end=end)
        self.registro = registro
        self.finestra = (pd.Timestamp(start), pd.Timestamp(end))

    def update_bars(self) -> list[MarketEvent]:
        eventi = super().update_bars()
        for evento in eventi:
            self.registro.append((self.finestra, pd.Timestamp(evento.timestamp)))
        return eventi

    def get_latest_bars(self, symbol: str, n: int = 1) -> pd.DataFrame:
        bars = super().get_latest_bars(symbol, n)
        if not bars.empty:
            self.registro.append((self.finestra, pd.Timestamp(bars.index.max())))
        return bars


def test_numero_di_finestre() -> None:
    finestre = walk_forward_windows("2005-01-01", "2014-12-31", train_years=5, test_years=1)
    assert len(finestre) == 5
    assert finestre[0][0] == pd.Timestamp("2005-01-01")
    assert finestre[0][2] == pd.Timestamp("2010-01-01")
    assert finestre[-1][3] == pd.Timestamp("2014-12-31")


def test_finestre_non_si_sovrappongono_nel_test() -> None:
    finestre = walk_forward_windows("2005-01-01", "2014-12-31")
    for precedente, successiva in zip(finestre, finestre[1:], strict=False):
        assert precedente[3] < successiva[2]
        assert precedente[1] < precedente[2]


def test_run_backtest_restituisce_metriche_ed_equity(walkforward_parquet_dir: Path) -> None:
    risultato = run_backtest(
        lambda: CompraSimbolo("AAA"),
        SIMBOLI,
        "2005-01-01",
        "2006-12-31",
        config=BacktestConfig(path=walkforward_parquet_dir),
    )
    assert risultato["metrics"]["rendimento_totale"] > 0.0
    assert len(risultato["equity_curve"]) > 400
    assert len(risultato["fills"]) == 1


def test_walk_forward_produce_una_riga_per_finestra(walkforward_parquet_dir: Path) -> None:
    tabella = walk_forward(
        factory_da_parametri,
        GRIGLIA,
        SIMBOLI,
        "2005-01-01",
        "2014-12-31",
        config=BacktestConfig(path=walkforward_parquet_dir),
    )
    assert len(tabella) == 5
    assert list(tabella["finestra"]) == [0, 1, 2, 3, 4]
    assert set(tabella["param_symbol"]) == {"AAA"}
    assert tabella.attrs["n_combinazioni"] == 2


def test_walk_forward_concatena_le_equity_di_test(walkforward_parquet_dir: Path) -> None:
    tabella = walk_forward(
        factory_da_parametri,
        GRIGLIA,
        SIMBOLI,
        "2005-01-01",
        "2014-12-31",
        config=BacktestConfig(path=walkforward_parquet_dir),
    )
    equity = tabella.attrs["equity_oos"]
    date = [t for t, _ in equity]
    assert date == sorted(date)
    assert pd.Timestamp(date[0]).year == 2010
    assert pd.Timestamp(date[-1]).year == 2014
    assert equity[-1][1] > equity[0][1]


def test_la_selezione_non_tocca_le_barre_di_test(walkforward_parquet_dir: Path) -> None:
    """Il registro dello spione dimostra che ogni finestra vede solo passato e proprio periodo.

    Il riscaldamento allarga la finestra all'indietro, mai in avanti: le date lette
    durante la scelta dei parametri restano tutte prima dell'inizio del test.
    """
    registro: list = []
    tabella = walk_forward(
        factory_da_parametri,
        GRIGLIA,
        SIMBOLI,
        "2005-01-01",
        "2014-12-31",
        config=BacktestConfig(path=walkforward_parquet_dir),
        data_handler_factory=lambda path, symbols, start, end: HandlerSpia(
            path, symbols, start, end, registro
        ),
    )
    assert registro

    for finestra, data in registro:
        assert data <= finestra[1]

    for riga in tabella.itertuples():
        accessi_train = [d for f, d in registro if f[1] == riga.train_fine]
        assert accessi_train
        assert max(accessi_train) < riga.test_inizio


def test_parameter_sensitivity_una_riga_per_combinazione(walkforward_parquet_dir: Path) -> None:
    tabella = parameter_sensitivity(
        factory_da_parametri,
        GRIGLIA,
        SIMBOLI,
        "2005-01-01",
        "2007-12-31",
        config=BacktestConfig(path=walkforward_parquet_dir),
    )
    assert len(tabella) == len(parameter_combinations(GRIGLIA)) == 2
    assert set(tabella["symbol"]) == {"AAA", "CCC"}
    migliore = tabella.sort_values("sharpe", ascending=False).iloc[0]
    assert migliore["symbol"] == "AAA"


def test_griglia_vuota_da_una_sola_combinazione() -> None:
    assert parameter_combinations({}) == [{}]
    assert len(parameter_combinations({"a": [1, 2], "b": [3, 4, 5]})) == 6


def test_deflated_sharpe_scende_con_il_numero_di_prove() -> None:
    comune = dict(sharpe_observed=0.1, n_obs=1000, skew=0.0, kurt=3.0)
    poche = deflated_sharpe(n_trials=2, **comune)
    molte = deflated_sharpe(n_trials=500, **comune)
    assert 0.0 <= molte < poche <= 1.0


def test_deflated_sharpe_penalizza_code_spesse_e_asimmetria_negativa() -> None:
    base = deflated_sharpe(0.1, 20, 1000, 0.0, 3.0)
    code_spesse = deflated_sharpe(0.1, 20, 1000, 0.0, 9.0)
    asimmetria_negativa = deflated_sharpe(0.1, 20, 1000, -1.5, 3.0)
    assert code_spesse < base
    assert asimmetria_negativa < base


def test_sharpe_massimo_atteso_cresce_con_le_prove() -> None:
    assert expected_max_sharpe(2) < expected_max_sharpe(50) < expected_max_sharpe(5000)
    assert expected_max_sharpe(100, variance_trials=0.0) == 0.0


def test_deflated_sharpe_su_serie_troppo_corta() -> None:
    assert deflated_sharpe(0.1, 10, 1, 0.0, 3.0) == 0.0
    assert deflated_sharpe(5.0, 10, 100, 0.0, 3.0) == pytest.approx(1.0, abs=1e-6)


def test_il_riscaldamento_non_conta_nelle_metriche(walkforward_parquet_dir: Path) -> None:
    """Le barre di riscaldamento servono alla strategia ma restano fuori dall'equity curve."""
    freddo = run_backtest(
        lambda: CompraSimbolo("AAA"),
        SIMBOLI,
        "2008-01-01",
        "2009-12-31",
        config=BacktestConfig(path=walkforward_parquet_dir),
    )
    caldo = run_backtest(
        lambda: CompraSimbolo("AAA"),
        SIMBOLI,
        "2008-01-01",
        "2009-12-31",
        config=BacktestConfig(path=walkforward_parquet_dir),
        warmup_start="2006-01-01",
    )
    assert pd.Timestamp(caldo["equity_curve"][0][0]).year == 2008
    assert len(caldo["equity_curve"]) == len(freddo["equity_curve"])
    assert caldo["fills"] == []
    assert len(freddo["fills"]) == 1


def test_argomenti_sciolti_ancora_accettati_ma_deprecati(walkforward_parquet_dir: Path) -> None:
    """Per una release i vecchi kwargs funzionano, con un avviso esplicito."""
    with pytest.warns(DeprecationWarning, match="BacktestConfig"):
        vecchio = run_backtest(
            lambda: CompraSimbolo("AAA"), SIMBOLI, "2005-01-01", "2006-12-31", path=walkforward_parquet_dir
        )
    nuovo = run_backtest(
        lambda: CompraSimbolo("AAA"),
        SIMBOLI,
        "2005-01-01",
        "2006-12-31",
        config=BacktestConfig(path=walkforward_parquet_dir),
    )
    assert vecchio["equity_curve"] == nuovo["equity_curve"]
    assert vecchio["config"] == nuovo["config"]


def test_parametro_sconosciuto_e_un_errore(walkforward_parquet_dir: Path) -> None:
    """Un refuso in un nome di parametro non deve passare in silenzio."""
    with pytest.raises(TypeError, match="slipage_bps"), pytest.warns(DeprecationWarning):
        run_backtest(
            lambda: CompraSimbolo("AAA"),
            SIMBOLI,
            "2005-01-01",
            "2006-12-31",
            config=BacktestConfig(path=walkforward_parquet_dir),
            slipage_bps=5.0,
        )
