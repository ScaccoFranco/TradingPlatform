"""Test del loop: un MarketEvent per data unica e nessun accesso oltre il cursore."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from quant.data import DataHandler, ParquetDataHandler
from quant.engine import Backtest
from quant.events import MarketEvent, SignalEvent
from quant.strategy import NoOpStrategy, Strategy

SYMBOLS = ["SPY", "QQQ", "TLT", "GLD"]


class RecordingStrategy(Strategy):
    """Registra i timestamp visti e l'ultima barra disponibile per ogni simbolo."""

    def __init__(self, data_handler: DataHandler, symbols: list[str]) -> None:
        super().__init__(data_handler)
        self.symbols = symbols
        self.seen: list[MarketEvent] = []
        self.last_bar_dates: list[pd.Timestamp] = []

    def on_bar(self, event: MarketEvent) -> list[SignalEvent]:
        """Legge le barre visibili e verifica che non superino il timestamp dell'evento."""
        self.seen.append(event)
        for symbol in self.symbols:
            bars = self.data_handler.get_latest_bars(symbol, 10)
            if not bars.empty:
                self.last_bar_dates.append(bars.index.max())
                assert bars.index.max() <= pd.Timestamp(event.timestamp)
        return []


def test_un_market_event_per_data_unica(parquet_dir: Path) -> None:
    handler = ParquetDataHandler(parquet_dir, SYMBOLS)
    date_uniche = len(handler.timeline)
    strategy = RecordingStrategy(handler, SYMBOLS)
    backtest = Backtest(handler, strategy)
    backtest.run()
    assert backtest.market_events == date_uniche == 20
    assert len(strategy.seen) == date_uniche
    assert [e.timestamp for e in strategy.seen] == list(handler.timeline.to_pydatetime())


def test_loop_termina_e_non_emette_altri_eventi(parquet_dir: Path) -> None:
    handler = ParquetDataHandler(parquet_dir, SYMBOLS)
    backtest = Backtest(handler, NoOpStrategy(handler))
    backtest.run()
    assert handler.continue_backtest is False
    assert handler.update_bars() == []
    assert (backtest.signal_events, backtest.order_events, backtest.fill_events) == (0, 0, 0)


def test_secondo_run_non_aggiunge_barre(parquet_dir: Path) -> None:
    handler = ParquetDataHandler(parquet_dir, SYMBOLS)
    backtest = Backtest(handler, NoOpStrategy(handler))
    backtest.run()
    backtest.run()
    assert backtest.market_events == 20
