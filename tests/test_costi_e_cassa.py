"""Test dei costi espliciti, del vincolo di cassa e delle cedole."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from quant.analysis import compute_metrics
from quant.data import ParquetDataHandler
from quant.engine import Backtest
from quant.execution import SimulatedExecutionHandler
from quant.portfolio import Portfolio
from quant.strategy import BuyAndHoldStrategy

CAPITALE = 100_000.0


def esegui(
    directory: Path,
    commissione: float = 0.0,
    slippage: float = 0.0,
    cash_buffer: float = 0.0,
    credit_dividends: bool = False,
) -> Portfolio:
    """Buy-and-hold su SPY con la configurazione di portafoglio indicata."""
    handler = ParquetDataHandler(directory, ["SPY"])
    portfolio = Portfolio(
        handler,
        initial_cash=CAPITALE,
        cash_buffer=cash_buffer,
        credit_dividends=credit_dividends,
    )
    execution = SimulatedExecutionHandler(handler, commission_per_trade=commissione, slippage_bps=slippage)
    Backtest(handler, BuyAndHoldStrategy(handler, "SPY"), portfolio, None, execution).run()
    return portfolio


def test_slippage_registrato_nei_fill_e_nelle_metriche(spy_parquet_dir: Path) -> None:
    portfolio = esegui(spy_parquet_dir, commissione=5.0, slippage=20.0)
    fill = portfolio.fills[0]
    barre = pd.read_parquet(spy_parquet_dir / "SPY.parquet")
    atteso = barre["open"].iloc[1] * 0.002 * fill.quantity
    assert fill.slippage_cost == pytest.approx(atteso)

    metriche = compute_metrics(portfolio.equity_curve, portfolio.fills)
    assert metriche["commissioni"] == 5.0
    assert metriche["slippage"] == pytest.approx(atteso)
    assert metriche["costi_totali"] == pytest.approx(atteso + 5.0)


def test_senza_slippage_il_costo_e_nullo(spy_parquet_dir: Path) -> None:
    portfolio = esegui(spy_parquet_dir)
    assert portfolio.fills[0].slippage_cost == 0.0
    assert compute_metrics(portfolio.equity_curve, portfolio.fills)["costi_totali"] == 0.0


def test_cassa_negativa_solo_senza_cuscinetto(gap_up_parquet_dir: Path) -> None:
    """Il gap fra close di dimensionamento e open di eseguito e' l'unico modo di sforare."""
    senza = esegui(gap_up_parquet_dir)
    assert senza.cash < 0.0

    con = esegui(gap_up_parquet_dir, cash_buffer=0.05)
    assert con.cash > 0.0
    assert con.positions["SPY"] < senza.positions["SPY"]


def test_la_cassa_limita_la_quantita_ordinata(spy_parquet_dir: Path) -> None:
    portfolio = esegui(spy_parquet_dir, cash_buffer=0.5)
    barre = pd.read_parquet(spy_parquet_dir / "SPY.parquet")
    assert portfolio.positions["SPY"] == int(CAPITALE * 0.5 // barre["close"].iloc[0])


def test_cedola_accreditata_in_cassa(dividend_parquet_dir: Path) -> None:
    con_cedole = esegui(dividend_parquet_dir, credit_dividends=True)
    senza_cedole = esegui(dividend_parquet_dir, credit_dividends=False)
    quantita = con_cedole.positions["SPY"]

    assert con_cedole.dividends_received == pytest.approx(quantita * 1.0)
    assert senza_cedole.dividends_received == 0.0
    assert con_cedole.equity_curve[-1][1] - senza_cedole.equity_curve[-1][1] == pytest.approx(quantita * 1.0)


def test_nessuna_cedola_fantasma_su_prezzi_lisci(spy_parquet_dir: Path) -> None:
    """adj_close proporzionale al close non deve generare accrediti."""
    portfolio = esegui(spy_parquet_dir, credit_dividends=True)
    assert portfolio.dividends_received == 0.0
