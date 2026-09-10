"""Test di portafoglio ed esecuzione: fill a T+1, costi, coerenza dell'equity curve."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from quant.data import ParquetDataHandler
from quant.engine import Backtest
from quant.execution import SimulatedExecutionHandler
from quant.portfolio import Portfolio
from quant.strategy import BuyAndHoldStrategy

CAPITALE = 100_000.0


def esegui(directory: Path, commissione: float = 0.0, slippage: float = 0.0) -> tuple[Portfolio, ParquetDataHandler]:
    """Buy-and-hold su SPY con i costi indicati."""
    handler = ParquetDataHandler(directory, ["SPY"])
    portfolio = Portfolio(handler, initial_cash=CAPITALE)
    execution = SimulatedExecutionHandler(handler, commission_per_trade=commissione, slippage_bps=slippage)
    backtest = Backtest(handler, BuyAndHoldStrategy(handler, "SPY"), portfolio, None, execution)
    backtest.run()
    return portfolio, handler


def prezzi(directory: Path) -> pd.DataFrame:
    """Barre grezze del file di test."""
    return pd.read_parquet(directory / "SPY.parquet")


def test_fill_all_open_della_barra_successiva(spy_parquet_dir: Path) -> None:
    portfolio, _ = esegui(spy_parquet_dir)
    barre = prezzi(spy_parquet_dir)
    assert len(portfolio.fills) == 1
    fill = portfolio.fills[0]
    assert pd.Timestamp(fill.timestamp) == barre.index[1]
    assert fill.fill_price == barre["open"].iloc[1]


def test_quantita_dimensionata_sul_close_della_barra_del_segnale(spy_parquet_dir: Path) -> None:
    portfolio, _ = esegui(spy_parquet_dir)
    barre = prezzi(spy_parquet_dir)
    assert portfolio.positions["SPY"] == int(CAPITALE // barre["close"].iloc[0])


def test_rendimento_coincide_con_il_buy_and_hold(spy_parquet_dir: Path) -> None:
    """Rendimento del portafoglio contro close_finale/open_secondo_giorno, al netto del cash residuo."""
    portfolio, _ = esegui(spy_parquet_dir)
    barre = prezzi(spy_parquet_dir)
    quantita = portfolio.positions["SPY"]
    investito = quantita * barre["open"].iloc[1]

    rendimento_portafoglio = portfolio.equity_curve[-1][1] / CAPITALE - 1.0
    rendimento_atteso = (investito / CAPITALE) * (barre["close"].iloc[-1] / barre["open"].iloc[1] - 1.0)
    assert abs(rendimento_portafoglio - rendimento_atteso) < 0.001
    assert portfolio.cash > 0.0


def test_i_costi_riducono_il_rendimento(spy_parquet_dir: Path) -> None:
    senza_costi, _ = esegui(spy_parquet_dir)
    con_commissione, _ = esegui(spy_parquet_dir, commissione=25.0)
    con_slippage, _ = esegui(spy_parquet_dir, slippage=10.0)
    assert con_commissione.equity_curve[-1][1] < senza_costi.equity_curve[-1][1]
    assert con_slippage.equity_curve[-1][1] < senza_costi.equity_curve[-1][1]


def test_equity_curve_ha_un_punto_per_barra(spy_parquet_dir: Path) -> None:
    portfolio, handler = esegui(spy_parquet_dir)
    assert len(portfolio.equity_curve) == len(handler.timeline)
    assert portfolio.equity_curve[0][1] == CAPITALE
