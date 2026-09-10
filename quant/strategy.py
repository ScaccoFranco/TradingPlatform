"""Strategie: leggono i dati solo via DataHandler ed emettono intenzioni, mai quantita'."""

from __future__ import annotations

from abc import ABC, abstractmethod

from quant.data import DataHandler
from quant.events import MarketEvent, SignalDirection, SignalEvent


class Strategy(ABC):
    """Interfaccia di una strategia guidata dagli eventi di mercato.

    Il data handler puo' essere collegato dopo la costruzione: serve a `quant.validation`,
    che ricrea la stessa strategia su finestre temporali diverse.
    """

    def __init__(self, data_handler: DataHandler | None = None) -> None:
        self.data_handler = data_handler

    @abstractmethod
    def on_bar(self, event: MarketEvent) -> list[SignalEvent]:
        """Reagisce a una nuova barra restituendo zero o piu' segnali."""


class NoOpStrategy(Strategy):
    """Strategia inerte: consuma le barre senza generare segnali."""

    def on_bar(self, event: MarketEvent) -> list[SignalEvent]:
        """Non emette alcun segnale."""
        return []


class BuyAndHoldStrategy(Strategy):
    """Compra il simbolo alla prima barra utile e non emette altro."""

    def __init__(self, data_handler: DataHandler, symbol: str, strength: float = 1.0) -> None:
        super().__init__(data_handler)
        self.symbol = symbol
        self.strength = strength
        self._emesso = False

    def on_bar(self, event: MarketEvent) -> list[SignalEvent]:
        """Un solo segnale LONG, alla prima barra in cui il simbolo ha dati."""
        if self._emesso or self.data_handler.get_latest_bars(self.symbol, 1).empty:
            return []
        self._emesso = True
        return [
            SignalEvent(
                timestamp=event.timestamp,
                symbol=self.symbol,
                direction=SignalDirection.LONG,
                strength=self.strength,
            )
        ]
