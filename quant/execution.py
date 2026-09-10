"""Esecuzione: gli ordini di oggi diventano eseguiti all'apertura della barra successiva."""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from quant.data import DataHandler
from quant.events import FillEvent, MarketEvent, OrderDirection, OrderEvent


class ExecutionHandler(ABC):
    """Interfaccia di esecuzione degli ordini."""

    @abstractmethod
    def on_order(self, order: OrderEvent) -> None:
        """Riceve un ordine gia' filtrato dal RiskManager."""

    @abstractmethod
    def on_market(self, event: MarketEvent) -> list[FillEvent]:
        """Esegue gli ordini pendenti sulla barra corrente."""


class SimulatedExecutionHandler(ExecutionHandler):
    """Fill all'open della barra T+1, con slippage sempre a sfavore del trader."""

    def __init__(
        self,
        data_handler: DataHandler,
        commission_per_trade: float = 0.0,
        slippage_bps: float = 0.0,
    ) -> None:
        self.data_handler = data_handler
        self.commission_per_trade = float(commission_per_trade)
        self.slippage_bps = float(slippage_bps)
        self.pending: list[OrderEvent] = []

    def on_order(self, order: OrderEvent) -> None:
        """Accoda l'ordine: non viene mai eseguito sulla barra che lo ha generato."""
        self.pending.append(order)

    def on_market(self, event: MarketEvent) -> list[FillEvent]:
        """Riempie gli ordini accodati all'open corrente; chi non ha barra oggi resta in coda."""
        if not self.pending:
            return []

        fills: list[FillEvent] = []
        ancora_pendenti: list[OrderEvent] = []
        for order in self.pending:
            open_price = self._open_price(order.symbol, event.timestamp)
            if open_price is None:
                ancora_pendenti.append(order)
                continue
            fill_price = self._apply_slippage(open_price, order.direction)
            fills.append(
                FillEvent(
                    timestamp=event.timestamp,
                    symbol=order.symbol,
                    direction=order.direction,
                    quantity=order.quantity,
                    fill_price=fill_price,
                    commission=self.commission_per_trade,
                    slippage_cost=abs(fill_price - open_price) * order.quantity,
                )
            )
        self.pending = ancora_pendenti
        return fills

    def _open_price(self, symbol: str, timestamp: object) -> float | None:
        """Open della barra corrente del simbolo, None se il simbolo oggi non scambia."""
        bars = self.data_handler.get_latest_bars(symbol, 1)
        if bars.empty or bars.index[-1] != pd.Timestamp(timestamp):
            return None
        return float(bars["open"].iloc[-1])

    def _apply_slippage(self, price: float, direction: OrderDirection) -> float:
        """Chi compra paga di piu', chi vende incassa di meno."""
        shift = self.slippage_bps / 10_000.0
        if direction is OrderDirection.BUY:
            return price * (1.0 + shift)
        return price * (1.0 - shift)
