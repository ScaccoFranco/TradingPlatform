"""Controlli di rischio: ogni ordine passa di qui prima dell'esecuzione."""

from __future__ import annotations

from quant.events import OrderDirection, OrderEvent
from quant.portfolio import Portfolio


class RiskManager:
    """Limita peso per simbolo, esposizione lorda e operativita' sotto drawdown."""

    def __init__(
        self,
        max_weight_per_symbol: float = 1.0,
        max_gross_exposure: float = 1.0,
        max_drawdown: float = 1.0,
    ) -> None:
        self.max_weight_per_symbol = float(max_weight_per_symbol)
        self.max_gross_exposure = float(max_gross_exposure)
        self.max_drawdown = float(max_drawdown)

    def filter(self, order: OrderEvent, portfolio: Portfolio) -> OrderEvent | None:
        """Riduce o rifiuta l'ordine; le riduzioni di posizione passano sempre."""
        price = portfolio.last_price(order.symbol)
        equity = portfolio.total_value()
        if price is None or price <= 0.0 or equity <= 0.0:
            return None

        current = portfolio.positions.get(order.symbol, 0)
        signed = order.quantity if order.direction is OrderDirection.BUY else -order.quantity
        target = current + signed
        if abs(target) <= abs(current):
            return order

        if self.current_drawdown(portfolio) > self.max_drawdown:
            return None

        target = self._cap_weight(target, price, equity)
        if self._gross_exposure_after(portfolio, order.symbol, target, price, equity) > self.max_gross_exposure:
            return None

        delta = target - current
        if delta == 0 or (delta > 0) != (signed > 0):
            return None
        if delta == signed:
            return order
        direction = OrderDirection.BUY if delta > 0 else OrderDirection.SELL
        return OrderEvent(
            timestamp=order.timestamp,
            symbol=order.symbol,
            direction=direction,
            quantity=abs(delta),
        )

    def current_drawdown(self, portfolio: Portfolio) -> float:
        """Drawdown corrente sull'equity curve gia' registrata, in frazione del picco."""
        if not portfolio.equity_curve:
            return 0.0
        picco = max(value for _, value in portfolio.equity_curve)
        corrente = portfolio.equity_curve[-1][1]
        if picco <= 0.0:
            return 0.0
        return max(0.0, (picco - corrente) / picco)

    def _cap_weight(self, target: int, price: float, equity: float) -> int:
        """Tronca la quantita' bersaglio al peso massimo per simbolo."""
        massimo = int(self.max_weight_per_symbol * equity // price)
        if abs(target) <= massimo:
            return target
        return massimo if target > 0 else -massimo

    def _gross_exposure_after(
        self,
        portfolio: Portfolio,
        symbol: str,
        target: int,
        price: float,
        equity: float,
    ) -> float:
        """Esposizione lorda che si avrebbe portando `symbol` a `target`.

        Gli altri simboli sono presi dalla proiezione post-ordini della barra: le
        vendite gia' decise liberano esposizione allo stesso open degli acquisti.
        """
        gross = abs(target * price)
        for altro, quantita in portfolio.projected_positions().items():
            if altro == symbol or quantita == 0:
                continue
            prezzo_altro = portfolio.last_price(altro)
            if prezzo_altro is not None:
                gross += abs(quantita * prezzo_altro)
        return gross / equity
