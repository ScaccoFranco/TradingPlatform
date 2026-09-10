"""Test end-to-end: tutti i componenti collegati nel loop."""

from __future__ import annotations

from pathlib import Path

from quant.data import ParquetDataHandler
from quant.engine import Backtest
from quant.execution import SimulatedExecutionHandler
from quant.portfolio import Portfolio
from quant.risk import RiskManager
from quant.strategy import BuyAndHoldStrategy

CAPITALE = 100_000.0


def test_il_risk_manager_taglia_lordine_dentro_il_motore(spy_parquet_dir: Path) -> None:
    handler = ParquetDataHandler(spy_parquet_dir, ["SPY"])
    portfolio = Portfolio(handler, initial_cash=CAPITALE)
    backtest = Backtest(
        handler,
        BuyAndHoldStrategy(handler, "SPY"),
        portfolio,
        RiskManager(max_weight_per_symbol=0.5),
        SimulatedExecutionHandler(handler),
    )
    backtest.run()

    prezzo_segnale = 100.0
    assert portfolio.positions["SPY"] == int(0.5 * CAPITALE // prezzo_segnale)
    assert portfolio.cash > CAPITALE * 0.45
    assert (backtest.signal_events, backtest.order_events, backtest.fill_events) == (1, 1, 1)


def test_esposizione_lorda_nulla_blocca_ogni_acquisto(spy_parquet_dir: Path) -> None:
    handler = ParquetDataHandler(spy_parquet_dir, ["SPY"])
    portfolio = Portfolio(handler, initial_cash=CAPITALE)
    backtest = Backtest(
        handler,
        BuyAndHoldStrategy(handler, "SPY"),
        portfolio,
        RiskManager(max_gross_exposure=0.0),
        SimulatedExecutionHandler(handler),
    )
    backtest.run()

    assert portfolio.positions.get("SPY", 0) == 0
    assert portfolio.cash == CAPITALE
    assert backtest.fill_events == 0


def test_momentum_ruota_le_posizioni_dentro_il_motore(momentum_bear_parquet_dir: Path) -> None:
    """Dal trend alle posizioni difensive: il motore esegue la rotazione completa."""
    from quant.strategies.momentum import CrossSectionalMomentum

    simboli = ["AAA", "BBB", "CCC", "DDD", "EEE", "SPY", "SHY"]
    handler = ParquetDataHandler(momentum_bear_parquet_dir, simboli)
    portfolio = Portfolio(handler, initial_cash=CAPITALE, cash_buffer=0.01)
    strategia = CrossSectionalMomentum(handler, simboli[:5], top_n=2)
    backtest = Backtest(
        handler,
        strategia,
        portfolio,
        RiskManager(max_weight_per_symbol=0.5, max_gross_exposure=1.0),
        SimulatedExecutionHandler(handler, commission_per_trade=1.0, slippage_bps=5.0),
    )
    backtest.run()

    assert portfolio.positions.get("SHY", 0) > 0
    assert portfolio.positions.get("AAA", 0) == 0
    assert portfolio.positions.get("BBB", 0) == 0
    assert portfolio.cash > 0.0
    assert len(portfolio.equity_curve) == len(handler.timeline)
    assert backtest.fill_events >= 4


def test_rotazione_completa_non_viene_bloccata_dallesposizione(momentum_parquet_dir: Path) -> None:
    """Vendere tutto e ricomprare altro sulla stessa barra non e' un raddoppio di esposizione."""
    from quant.events import OrderDirection, SignalDirection, SignalEvent

    simboli = ["AAA", "BBB", "CCC", "DDD", "EEE", "SPY", "SHY"]
    handler = ParquetDataHandler(momentum_parquet_dir, simboli)
    for _ in range(30):
        handler.update_bars()

    portfolio = Portfolio(handler, initial_cash=CAPITALE)
    portfolio.on_market(handler.update_bars()[0])
    prezzo_aaa = portfolio.last_price("AAA")
    assert prezzo_aaa is not None
    portfolio.positions["AAA"] = int(CAPITALE // prezzo_aaa)
    portfolio.cash = CAPITALE - portfolio.positions["AAA"] * prezzo_aaa

    manager = RiskManager(max_weight_per_symbol=1.0, max_gross_exposure=1.0)
    timestamp = handler.current_timestamp()
    assert timestamp is not None

    vendita = portfolio.on_signal(SignalEvent(timestamp, "AAA", SignalDirection.EXIT, 0.0))
    assert vendita is not None and vendita.direction is OrderDirection.SELL
    assert manager.filter(vendita, portfolio) is vendita

    acquisto = portfolio.on_signal(SignalEvent(timestamp, "BBB", SignalDirection.LONG, 1.0))
    assert acquisto is not None and acquisto.direction is OrderDirection.BUY
    filtrato = manager.filter(acquisto, portfolio)
    assert filtrato is not None
    assert filtrato.quantity > 0
