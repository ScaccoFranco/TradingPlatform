"""Test del RiskManager: peso massimo, esposizione lorda, blocco su drawdown."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from quant.data import ParquetDataHandler
from quant.events import OrderDirection, OrderEvent, SignalDirection, SignalEvent
from quant.portfolio import Portfolio
from quant.risk import RiskManager

CAPITALE = 100_000.0


def contesto(directory: Path) -> tuple[ParquetDataHandler, Portfolio]:
    """Handler posizionato sulla prima barra e portafoglio con equity segnata."""
    handler = ParquetDataHandler(directory, ["SPY"])
    handler.update_bars()
    portfolio = Portfolio(handler, initial_cash=CAPITALE)
    portfolio.equity_curve.append((datetime(2020, 1, 1), CAPITALE))
    return handler, portfolio


def ordine_pieno(portfolio: Portfolio) -> OrderEvent:
    """Ordine generato da un segnale LONG a forza piena."""
    segnale = SignalEvent(datetime(2020, 1, 1), "SPY", SignalDirection.LONG, 1.0)
    ordine = portfolio.on_signal(segnale)
    assert ordine is not None
    return ordine


def test_peso_massimo_dimezza_la_quantita(spy_parquet_dir: Path) -> None:
    _, portfolio = contesto(spy_parquet_dir)
    ordine = ordine_pieno(portfolio)
    filtrato = RiskManager(max_weight_per_symbol=0.5).filter(ordine, portfolio)
    assert filtrato is not None
    prezzo = portfolio.last_price("SPY")
    assert prezzo is not None
    assert filtrato.quantity == int(0.5 * CAPITALE // prezzo)
    assert filtrato.quantity * prezzo / CAPITALE <= 0.5


def test_ordine_entro_i_limiti_passa_invariato(spy_parquet_dir: Path) -> None:
    _, portfolio = contesto(spy_parquet_dir)
    ordine = ordine_pieno(portfolio)
    assert RiskManager(max_weight_per_symbol=1.0).filter(ordine, portfolio) is ordine


def test_esposizione_lorda_rifiuta_lordine(spy_parquet_dir: Path) -> None:
    _, portfolio = contesto(spy_parquet_dir)
    ordine = ordine_pieno(portfolio)
    manager = RiskManager(max_weight_per_symbol=1.0, max_gross_exposure=0.3)
    assert manager.filter(ordine, portfolio) is None


def test_drawdown_oltre_soglia_rifiuta_lordine(spy_parquet_dir: Path) -> None:
    _, portfolio = contesto(spy_parquet_dir)
    ordine = ordine_pieno(portfolio)
    portfolio.equity_curve.append((datetime(2020, 1, 2), CAPITALE * 0.75))
    manager = RiskManager(max_drawdown=0.20)
    assert manager.current_drawdown(portfolio) == 0.25
    assert manager.filter(ordine, portfolio) is None


def test_le_chiusure_passano_anche_in_drawdown(spy_parquet_dir: Path) -> None:
    """Il blocco vale per chi aumenta il rischio, mai per chi lo riduce."""
    _, portfolio = contesto(spy_parquet_dir)
    portfolio.positions["SPY"] = 500
    portfolio.equity_curve.append((datetime(2020, 1, 2), CAPITALE * 0.5))
    vendita = OrderEvent(datetime(2020, 1, 2), "SPY", OrderDirection.SELL, 500)
    assert RiskManager(max_drawdown=0.10).filter(vendita, portfolio) is vendita
