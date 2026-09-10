"""Accesso ai dati storici senza look-ahead: il cursore non supera mai la barra corrente."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

import pandas as pd

from quant.events import MarketEvent

COLUMNS = ("open", "high", "low", "close", "adj_close", "volume")


class DataHandler(ABC):
    """Interfaccia dati: espone solo le barre fino al cursore corrente."""

    continue_backtest: bool

    @abstractmethod
    def update_bars(self) -> list[MarketEvent]:
        """Avanza il cursore di una barra e restituisce gli eventi generati."""

    @abstractmethod
    def get_latest_bars(self, symbol: str, n: int = 1) -> pd.DataFrame:
        """Ultime n barre del simbolo fino al cursore incluso, mai oltre."""

    @abstractmethod
    def get_latest_bars_all(self, n: int = 1) -> dict[str, pd.DataFrame]:
        """Ultime n barre di ogni simbolo fino al cursore incluso, mai oltre."""

    @abstractmethod
    def emitted_timeline(self) -> pd.DatetimeIndex:
        """Date gia' emesse, fino al cursore incluso: nessuna data futura."""

    @abstractmethod
    def current_timestamp(self) -> datetime | None:
        """Timestamp del cursore, None se il backtest non e' ancora partito."""


class ParquetDataHandler(DataHandler):
    """DataHandler su file Parquet, uno per simbolo, con indice temporale unificato."""

    def __init__(
        self,
        path: str | Path,
        symbols: list[str],
        start: str | datetime | None = None,
        end: str | datetime | None = None,
    ) -> None:
        self.symbols = list(symbols)
        self._data: dict[str, pd.DataFrame] = {}
        for symbol in self.symbols:
            frame = pd.read_parquet(Path(path) / f"{symbol}.parquet")
            frame = frame.sort_index()
            if start is not None:
                frame = frame[frame.index >= pd.Timestamp(start)]
            if end is not None:
                frame = frame[frame.index <= pd.Timestamp(end)]
            self._data[symbol] = frame

        index = pd.DatetimeIndex([])
        for frame in self._data.values():
            index = index.union(pd.DatetimeIndex(frame.index))
        self._timeline: pd.DatetimeIndex = index.sort_values()
        self._cursor = -1
        self.continue_backtest = len(self._timeline) > 0

    def update_bars(self) -> list[MarketEvent]:
        """Avanza al timestamp successivo e emette un MarketEvent, nessuno se i dati sono finiti."""
        if not self.continue_backtest:
            return []
        self._cursor += 1
        if self._cursor >= len(self._timeline) - 1:
            self.continue_backtest = False
        return [MarketEvent(timestamp=self._timeline[self._cursor].to_pydatetime())]

    def get_latest_bars(self, symbol: str, n: int = 1) -> pd.DataFrame:
        """Ultime n barre effettivamente presenti fino al cursore, senza forward-fill.

        La ricerca del cursore e' binaria sull'indice ordinato: su una serie lunga
        costa quanto un logaritmo invece di scandire tutte le date a ogni barra.
        """
        frame = self._data[symbol]
        if self._cursor < 0 or n <= 0:
            return frame.iloc[:0]
        current = self._timeline[self._cursor]
        visibili = int(frame.index.searchsorted(current, side="right"))
        if visibili == 0:
            return frame.iloc[:0]
        return frame.iloc[max(0, visibili - n) : visibili]

    def get_latest_bars_all(self, n: int = 1) -> dict[str, pd.DataFrame]:
        """Ultime n barre di ogni simbolo caricato, ognuna troncata al cursore."""
        return {symbol: self.get_latest_bars(symbol, n) for symbol in self.symbols}

    def emitted_timeline(self) -> pd.DatetimeIndex:
        """Porzione della timeline gia' emessa: le date oltre il cursore restano invisibili."""
        if self._cursor < 0:
            return self._timeline[:0]
        return self._timeline[: self._cursor + 1]

    def previous_timestamp(self) -> datetime | None:
        """Timestamp della barra precedente a quella corrente, None se non esiste."""
        if self._cursor < 1:
            return None
        return self._timeline[self._cursor - 1].to_pydatetime()

    def current_timestamp(self) -> datetime | None:
        """Timestamp del cursore corrente."""
        if self._cursor < 0:
            return None
        return self._timeline[self._cursor].to_pydatetime()

    def has_bar(self, symbol: str) -> bool:
        """True se il simbolo ha una barra proprio al timestamp del cursore."""
        if self._cursor < 0:
            return False
        return self._timeline[self._cursor] in self._data[symbol].index

    @property
    def timeline(self) -> pd.DatetimeIndex:
        """Indice temporale unificato dei simboli caricati."""
        return self._timeline
