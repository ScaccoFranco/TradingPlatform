"""Eventi scambiati nel loop del backtest."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class SignalDirection(StrEnum):
    """Direzione di un'intenzione di trading."""

    LONG = "LONG"
    SHORT = "SHORT"
    EXIT = "EXIT"


class OrderDirection(StrEnum):
    """Verso di un ordine sul mercato."""

    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True, slots=True)
class MarketEvent:
    """Nuova barra disponibile: il cursore del DataHandler e' avanzato al timestamp indicato.

    Non porta con se' i prezzi: la strategia li legge solo via `get_latest_bars`.
    """

    timestamp: datetime


@dataclass(frozen=True, slots=True)
class SignalEvent:
    """Intenzione della strategia: direzione e forza in [0, 1], mai una quantita'."""

    timestamp: datetime
    symbol: str
    direction: SignalDirection
    strength: float


@dataclass(frozen=True, slots=True)
class OrderEvent:
    """Ordine a mercato dimensionato dal Portfolio, in attesa del filtro del RiskManager."""

    timestamp: datetime
    symbol: str
    direction: OrderDirection
    quantity: int


@dataclass(frozen=True, slots=True)
class FillEvent:
    """Eseguito restituito dall'ExecutionHandler, con prezzo effettivo e costi.

    `slippage_cost` e' la differenza in valuta fra il prezzo pagato e il prezzo di
    riferimento della barra: serve a rendere i costi totali confrontabili con le
    commissioni invece di restare nascosti dentro `fill_price`.
    """

    timestamp: datetime
    symbol: str
    direction: OrderDirection
    quantity: int
    fill_price: float
    commission: float
    slippage_cost: float = 0.0


type Event = MarketEvent | SignalEvent | OrderEvent | FillEvent
