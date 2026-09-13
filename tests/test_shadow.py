"""Test dello shadow backtest."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from quant.data import ParquetDataHandler
from quant.engine import Backtest
from quant.execution import SimulatedExecutionHandler
from quant.portfolio import Portfolio
from quant.risk import RiskManager
from quant.shadow import ShadowBacktest
from quant.strategy import BuyAndHoldStrategy

CAPITALE = 100_000.0
COMMISSIONE = 1.0
SLIPPAGE = 5.0


def backtest_diretto(directory: Path) -> Portfolio:
    """Backtest costruito a mano, senza passare dallo shadow."""
    handler = ParquetDataHandler(directory, ["SPY"])
    portfolio = Portfolio(handler, initial_cash=CAPITALE, cash_buffer=0.01, credit_dividends=True)
    Backtest(
        handler,
        BuyAndHoldStrategy(handler, "SPY"),
        portfolio,
        RiskManager(),
        SimulatedExecutionHandler(handler, COMMISSIONE, SLIPPAGE),
    ).run()
    return portfolio


def test_shadow_riproduce_gli_stessi_fill(spy_parquet_dir: Path) -> None:
    """Il paragone deve essere identico al backtest, altrimenti misura se stesso."""
    barre = pd.read_parquet(spy_parquet_dir / "SPY.parquet")
    ombra = ShadowBacktest(
        lambda: BuyAndHoldStrategy(None, "SPY"),
        start_date=barre.index[0].date(),
        symbols=["SPY"],
        path=spy_parquet_dir,
        initial_cash=CAPITALE,
        commission_per_trade=COMMISSIONE,
        slippage_bps=SLIPPAGE,
        warmup_days=0,
    ).run(end=barre.index[-1].date())

    atteso = backtest_diretto(spy_parquet_dir)
    assert [(f.timestamp, f.symbol, f.quantity, f.fill_price) for f in ombra.fills] == [
        (f.timestamp, f.symbol, f.quantity, f.fill_price) for f in atteso.fills
    ]
    assert ombra.equity_curve == atteso.equity_curve
    assert ombra.metrics["equity_finale"] == atteso.equity_curve[-1][1]


def test_shadow_riscaldato_parte_dalla_data_di_avvio(momentum_parquet_dir: Path) -> None:
    """Con riscaldamento la strategia arriva informata ma le metriche partono dal live."""
    from quant.strategies.momentum import CrossSectionalMomentum

    universo = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    ombra = ShadowBacktest(
        lambda: CrossSectionalMomentum(None, universo, top_n=2, cash_symbol="SHY"),
        start_date="2016-06-01",
        symbols=[*universo, "SPY", "SHY"],
        path=momentum_parquet_dir,
        warmup_days=400,
    ).run(end="2016-11-30")

    assert ombra.equity_curve
    assert pd.Timestamp(ombra.equity_curve[0][0]).date() >= ombra.start
    assert all(pd.Timestamp(f.timestamp).date() >= ombra.start for f in ombra.fills)
    assert ombra.equity_curve[0][1] == 100_000.0


def test_serie_di_equity_indicizzata_per_data(spy_parquet_dir: Path) -> None:
    barre = pd.read_parquet(spy_parquet_dir / "SPY.parquet")
    ombra = ShadowBacktest(
        lambda: BuyAndHoldStrategy(None, "SPY"),
        start_date=barre.index[0].date(),
        symbols=["SPY"],
        path=spy_parquet_dir,
        warmup_days=0,
    ).run(end=barre.index[-1].date())

    serie = ombra.equity_series()
    assert len(serie) == len(barre)
    assert serie.index.max() == barre.index.max()
