"""Portafoglio: dimensiona i segnali, applica gli eseguiti e traccia l'equity curve."""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from quant.data import DataHandler
from quant.events import (
    FillEvent,
    MarketEvent,
    OrderDirection,
    OrderEvent,
    SignalDirection,
    SignalEvent,
)

EPS_DIVIDENDO = 1e-5  # sotto questa frazione del prezzo il residuo e' rumore numerico, non cedola


class Portfolio:
    """Unico componente che conosce il capitale e trasforma le intenzioni in quantita'."""

    def __init__(
        self,
        data_handler: DataHandler,
        initial_cash: float = 100_000.0,
        max_weight: float = 1.0,
        cash_buffer: float = 0.0,
        credit_dividends: bool = False,
    ) -> None:
        self.data_handler = data_handler
        self.initial_cash = float(initial_cash)
        self.cash = float(initial_cash)
        self.max_weight = float(max_weight)
        self.cash_buffer = float(cash_buffer)
        self.credit_dividends = credit_dividends
        self.positions: dict[str, int] = {}
        self.equity_curve: list[tuple[datetime, float]] = []
        self.fills: list[FillEvent] = []
        self.dividends_received = 0.0
        self._cassa_attesa = 0.0
        self._delta_attesi: dict[str, int] = {}

    def on_market(self, event: MarketEvent) -> None:
        """Accredita le cedole, poi mark-to-market sui close e nuovo punto di equity curve."""
        self._cassa_attesa = 0.0
        self._delta_attesi = {}
        if self.credit_dividends:
            self._credit_dividends(event)
        self.equity_curve.append((event.timestamp, self.total_value()))

    def on_signal(self, event: SignalEvent) -> OrderEvent | None:
        """Traduce forza e direzione in un ordine per la differenza dalla posizione attuale."""
        price = self.last_price(event.symbol)
        if price is None or price <= 0.0:
            return None

        current = self.positions.get(event.symbol, 0)
        target = self._target_quantity(event, price)
        delta = target - current
        if delta > 0:
            delta = min(delta, self._max_affordable(price))
        if delta == 0:
            return None

        self._cassa_attesa -= delta * price
        self._delta_attesi[event.symbol] = self._delta_attesi.get(event.symbol, 0) + delta
        direction = OrderDirection.BUY if delta > 0 else OrderDirection.SELL
        return OrderEvent(
            timestamp=event.timestamp,
            symbol=event.symbol,
            direction=direction,
            quantity=abs(delta),
        )

    def on_fill(self, event: FillEvent) -> None:
        """Aggiorna cassa e posizioni con il prezzo effettivo e la commissione pagata."""
        signed = event.quantity if event.direction is OrderDirection.BUY else -event.quantity
        self.positions[event.symbol] = self.positions.get(event.symbol, 0) + signed
        self.cash -= signed * event.fill_price
        self.cash -= event.commission
        self.fills.append(event)

    def _target_quantity(self, event: SignalEvent, price: float) -> int:
        """Quantita' bersaglio: forza x peso massimo x valore del portafoglio, su prezzo non aggiustato."""
        if event.direction is SignalDirection.EXIT:
            return 0
        strength = max(0.0, min(1.0, event.strength))
        target_value = strength * self.max_weight * self.total_value()
        quantity = int(target_value // price)
        return quantity if event.direction is SignalDirection.LONG else -quantity

    def _max_affordable(self, price: float) -> int:
        """Quante azioni copre la cassa disponibile, al netto del cuscinetto richiesto.

        Conta anche gli ordini gia' emessi su questa barra: le vendite di un
        ribilanciamento liberano cassa allo stesso open in cui avvengono gli acquisti,
        altrimenti la rotazione mensile resterebbe bloccata dal contante fermo.
        Il dimensionamento usa il close di oggi mentre il fill avviene all'open di
        domani: con `cash_buffer` a zero un'apertura in gap al rialzo puo' ancora
        lasciare la cassa di poco negativa, un cuscinetto anche minimo lo evita.
        """
        disponibile = max(0.0, self.cash + self._cassa_attesa) * (1.0 - self.cash_buffer)
        return int(disponibile // price)

    def _credit_dividends(self, event: MarketEvent) -> None:
        """Accredita in cassa la cedola implicita nel rapporto fra adj_close e close.

        Con adj_t / adj_(t-1) = (close_t + cedola) / close_(t-1) si ricava la cedola
        per azione: e' il modo in cui il vincolo "i rendimenti usano adj_close" entra
        nel portafoglio senza toccare i prezzi di eseguito, che restano non aggiustati.
        La cedola resta in cassa e viene reinvestita solo al ribilanciamento successivo,
        quindi il rendimento e' piu' basso di quello implicito in adj_close, che assume
        reinvestimento immediato allo stacco.
        """
        for symbol, quantity in self.positions.items():
            if quantity == 0:
                continue
            bars = self.data_handler.get_latest_bars(symbol, 2)
            if len(bars) < 2 or bars.index[-1] != pd.Timestamp(event.timestamp):
                continue
            precedente = bars.iloc[-2]
            corrente = bars.iloc[-1]
            if precedente["adj_close"] <= 0.0 or precedente["close"] <= 0.0:
                continue
            atteso = precedente["close"] * (corrente["adj_close"] / precedente["adj_close"])
            cedola = float(atteso - corrente["close"])
            if cedola > EPS_DIVIDENDO * float(corrente["close"]):
                self.cash += quantity * cedola
                self.dividends_received += quantity * cedola

    def last_price(self, symbol: str) -> float | None:
        """Ultimo close non aggiustato visibile per il simbolo, None se non c'e' ancora."""
        bars = self.data_handler.get_latest_bars(symbol, 1)
        if bars.empty:
            return None
        return float(bars["close"].iloc[-1])

    def projected_positions(self) -> dict[str, int]:
        """Posizioni che si avranno dopo gli ordini gia' emessi su questa barra.

        Vendite e acquisti di un ribilanciamento vengono eseguiti allo stesso open,
        quindi chi valuta un limite deve guardare il portafoglio risultante, non
        quello di ieri: altrimenti una rotazione completa sembra un raddoppio.
        Il RiskManager puo' ancora ridurre un ordine gia' emesso, quindi la stima e'
        prudente per eccesso.
        """
        proiezione = dict(self.positions)
        for symbol, delta in self._delta_attesi.items():
            proiezione[symbol] = proiezione.get(symbol, 0) + delta
        return proiezione

    def positions_value(self) -> float:
        """Valore di mercato delle posizioni aperte."""
        total = 0.0
        for symbol, quantity in self.positions.items():
            if quantity == 0:
                continue
            price = self.last_price(symbol)
            if price is not None:
                total += quantity * price
        return total

    def total_value(self) -> float:
        """Cassa piu' valore delle posizioni."""
        return self.cash + self.positions_value()

    def gross_exposure(self) -> float:
        """Esposizione lorda in frazione dell'equity."""
        equity = self.total_value()
        if equity <= 0.0:
            return 0.0
        gross = 0.0
        for symbol, quantity in self.positions.items():
            if quantity == 0:
                continue
            price = self.last_price(symbol)
            if price is not None:
                gross += abs(quantity * price)
        return gross / equity

    def weight(self, symbol: str) -> float:
        """Peso corrente del simbolo sull'equity."""
        equity = self.total_value()
        price = self.last_price(symbol)
        if equity <= 0.0 or price is None:
            return 0.0
        return self.positions.get(symbol, 0) * price / equity
