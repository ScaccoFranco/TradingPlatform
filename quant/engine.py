"""Loop degli eventi del backtest."""

from __future__ import annotations

from collections import deque
from typing import Any, Protocol

from quant.data import DataHandler
from quant.events import (
    Event,
    FillEvent,
    MarketEvent,
    OrderEvent,
    SignalEvent,
)
from quant.logging import get_logger
from quant.strategy import Strategy

logger = get_logger("engine")


class PortfolioLike(Protocol):
    """Contratto minimo richiesto dal loop a un portafoglio."""

    def on_market(self, event: MarketEvent) -> None: ...

    def on_signal(self, event: SignalEvent) -> OrderEvent | None: ...

    def on_fill(self, event: FillEvent) -> None: ...


class RiskManagerLike(Protocol):
    """Contratto minimo richiesto dal loop a un gestore del rischio."""

    def filter(self, order: OrderEvent, portfolio: Any) -> OrderEvent | None: ...


class ExecutionHandlerLike(Protocol):
    """Contratto minimo richiesto dal loop a un gestore dell'esecuzione."""

    def on_order(self, order: OrderEvent) -> None: ...

    def on_market(self, event: MarketEvent) -> list[FillEvent]: ...


class Backtest:
    """Orchestratore: avanza le barre e smista gli eventi ai componenti."""

    def __init__(
        self,
        data_handler: DataHandler,
        strategy: Strategy,
        portfolio: PortfolioLike | None = None,
        risk_manager: RiskManagerLike | None = None,
        execution_handler: ExecutionHandlerLike | None = None,
    ) -> None:
        self.data_handler = data_handler
        self.strategy = strategy
        self.portfolio = portfolio
        self.risk_manager = risk_manager
        self.execution_handler = execution_handler
        self.queue: deque[Event] = deque()
        self.market_events = 0
        self.signal_events = 0
        self.order_events = 0
        self.fill_events = 0

    def run(self) -> None:
        """Cicla finche' ci sono barre: aggiorna i dati e svuota la coda a ogni passo."""
        while self.data_handler.continue_backtest:
            self.queue.extend(self.data_handler.update_bars())
            prima = (self.signal_events, self.order_events, self.fill_events)
            self._drain()
            logger.debug(
                "barra_conclusa",
                timestamp=str(self.data_handler.current_timestamp()),
                signals=self.signal_events - prima[0],
                orders=self.order_events - prima[1],
                fills=self.fill_events - prima[2],
            )

    def _drain(self) -> None:
        """Consuma la coda dispatchando ogni evento per tipo."""
        while self.queue:
            self._dispatch(self.queue.popleft())

    def _dispatch(self, event: Event) -> None:
        """Instrada un singolo evento al componente competente."""
        match event:
            case MarketEvent():
                self._on_market(event)
            case SignalEvent():
                self._on_signal(event)
            case OrderEvent():
                self._on_order(event)
            case FillEvent():
                self._on_fill(event)

    def _on_market(self, event: MarketEvent) -> None:
        """Ordine fisso della barra: fill all'open, mark-to-market sul close, poi la strategia.

        I fill precedono il mark-to-market perche' avvengono all'apertura, quindi la
        posizione va gia' valorizzata nel punto di equity di questa barra.
        """
        self.market_events += 1
        if self.execution_handler is not None:
            for fill in self.execution_handler.on_market(event):
                self._on_fill(fill)
        if self.portfolio is not None:
            self.portfolio.on_market(event)
        self.queue.extend(self.strategy.on_bar(event))

    def _on_signal(self, event: SignalEvent) -> None:
        """Trasforma il segnale in ordine tramite il Portfolio."""
        self.signal_events += 1
        if self.portfolio is None:
            return
        order = self.portfolio.on_signal(event)
        if order is not None:
            self.queue.append(order)

    def _on_order(self, event: OrderEvent) -> None:
        """Filtra l'ordine con il RiskManager prima di inoltrarlo all'esecuzione."""
        self.order_events += 1
        order: OrderEvent | None = event
        if self.risk_manager is not None and self.portfolio is not None:
            order = self.risk_manager.filter(event, self.portfolio)
        if order is not None and self.execution_handler is not None:
            self.execution_handler.on_order(order)

    def _on_fill(self, event: FillEvent) -> None:
        """Aggiorna il Portfolio con l'eseguito."""
        self.fill_events += 1
        logger.info(
            "fill",
            timestamp=str(event.timestamp),
            symbol=event.symbol,
            direction=str(event.direction),
            quantity=event.quantity,
            fill_price=event.fill_price,
        )
        if self.portfolio is not None:
            self.portfolio.on_fill(event)
