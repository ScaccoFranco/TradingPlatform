"""Portafoglio a pesi fissi, ribilanciato alla prima barra di ogni mese."""

from __future__ import annotations

from quant.calendar import is_first_trading_day_of_month
from quant.data import DataHandler
from quant.events import MarketEvent, SignalDirection, SignalEvent
from quant.strategy import Strategy


class FixedWeightsStrategy(Strategy):
    """Benchmark statico: ogni simbolo torna al suo peso a ogni ribilanciamento.

    Serve a far girare un 60/40 nello stesso motore e con gli stessi costi della
    strategia attiva, cosi' il confronto non regala nulla a nessuno dei due.
    """

    def __init__(
        self,
        data_handler: DataHandler | None = None,
        weights: dict[str, float] | None = None,
        rebalance_monthly: bool = True,
    ) -> None:
        super().__init__(data_handler)
        self.weights = dict(weights or {})
        self.rebalance_monthly = rebalance_monthly
        self._investito = False

    def on_bar(self, event: MarketEvent) -> list[SignalEvent]:
        """Riporta ogni simbolo al peso obiettivo, mensilmente o una volta sola."""
        if self.rebalance_monthly:
            if not is_first_trading_day_of_month(event.timestamp, self.data_handler):
                return []
        elif self._investito:
            return []

        segnali = [
            SignalEvent(event.timestamp, symbol, SignalDirection.LONG, peso)
            for symbol, peso in sorted(self.weights.items())
            if not self.data_handler.get_latest_bars(symbol, 1).empty
        ]
        if segnali:
            self._investito = True
        return segnali
