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
from quant.logging import get_logger

logger = get_logger("portfolio")

def _valore(barra: pd.Series, colonna: str, default: float) -> float:
    """Legge un campo della barra tollerando colonne assenti o valori mancanti."""
    valore = barra.get(colonna, default)
    if valore is None or pd.isna(valore):
        return default
    return float(valore)


class Portfolio:
    """Unico componente che conosce il capitale e trasforma le intenzioni in quantita'."""

    def __init__(
        self,
        data_handler: DataHandler,
        initial_cash: float = 100_000.0,
        max_weight: float = 1.0,
        cash_buffer: float = 0.0,
        credit_dividends: bool = False,
        stale_after_days: int = 5,
    ) -> None:
        self.data_handler = data_handler
        self.initial_cash = float(initial_cash)
        self.cash = float(initial_cash)
        self.max_weight = float(max_weight)
        self.cash_buffer = float(cash_buffer)
        self.credit_dividends = credit_dividends
        self.stale_after_days = stale_after_days
        self.stale_symbols: set[str] = set()
        self.positions: dict[str, int] = {}
        self.equity_curve: list[tuple[datetime, float]] = []
        self.peak_equity = float(initial_cash)
        self.fills: list[FillEvent] = []
        self.dividends_received = 0.0
        self._cassa_attesa = 0.0
        self._delta_attesi: dict[str, int] = {}

    def on_market(self, event: MarketEvent) -> None:
        """Operazioni sul capitale, poi mark-to-market sui close e punto di equity curve.

        L'ordine conta: prima gli split cambiano il numero di azioni, poi le cedole
        entrano in cassa, solo alla fine si valorizza il portafoglio.
        """
        self._cassa_attesa = 0.0
        self._delta_attesi = {}
        self._apply_splits(event)
        if self.credit_dividends:
            self._credit_dividends(event)
        self._segnala_posizioni_ferme(event)
        valore = self.total_value()
        self.equity_curve.append((event.timestamp, valore))
        self.peak_equity = max(self.peak_equity, valore)

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

    def _apply_splits(self, event: MarketEvent) -> None:
        """Riscala le posizioni dei simboli che frazionano oggi, liquidando il residuo.

        I prezzi in archivio sono grezzi, quindi allo split il prezzo scende e le azioni
        devono salire, come fa il broker. Il rapporto si applica alla posizione com'era
        alla chiusura precedente: gli eseguiti di oggi avvengono gia' nella scala nuova.
        La frazione di azione che avanza viene monetizzata al prezzo di apertura.
        """
        for symbol in list(self.positions):
            barra = self._barra_corrente(symbol, event)
            if barra is None:
                continue
            fattore = _valore(barra, "split_factor", 1.0)
            precedente = self._quantita_precedente(symbol, event)
            if fattore == 1.0 or precedente == 0:
                continue

            esatto = precedente * fattore
            intere = int(esatto)
            residuo = esatto - intere
            eseguiti_oggi = self.positions.get(symbol, 0) - precedente
            self.positions[symbol] = intere + eseguiti_oggi
            if residuo:
                self.cash += residuo * _valore(barra, "open", _valore(barra, "close", 0.0))
            logger.info(
                "split_applicato",
                symbol=symbol,
                factor=fattore,
                before=precedente,
                after=self.positions[symbol],
                residuo=round(residuo, 6),
            )

    def _credit_dividends(self, event: MarketEvent) -> None:
        """Accredita in cassa la cedola staccata oggi, letta dalla colonna `dividends`.

        La cedola e' per azione e nella stessa scala dei prezzi grezzi, quindi va
        moltiplicata per le azioni possedute dopo l'eventuale split della giornata.
        """
        for symbol, quantity in self.positions.items():
            if quantity == 0:
                continue
            barra = self._barra_corrente(symbol, event)
            if barra is None:
                continue
            cedola = _valore(barra, "dividends", 0.0)
            if cedola <= 0.0:
                continue
            self.cash += quantity * cedola
            self.dividends_received += quantity * cedola

    def _segnala_posizioni_ferme(self, event: MarketEvent) -> None:
        """Avvisa quando una posizione aperta smette di avere barre.

        Un titolo che sparisce dai dati resta valorizzato all'ultimo prezzo noto, che
        col passare dei giorni e' una finzione: meglio dirlo una volta per simbolo,
        appena il ritardo supera la soglia, che scoprirlo dall'equity curve.
        """
        oggi = pd.Timestamp(event.timestamp)
        for symbol, quantity in self.positions.items():
            if quantity == 0:
                self.stale_symbols.discard(symbol)
                continue
            barre = self.data_handler.get_latest_bars(symbol, 1)
            ritardo = None if barre.empty else (oggi - barre.index[-1]).days
            ferma = ritardo is None or ritardo > self.stale_after_days
            if ferma and symbol not in self.stale_symbols:
                self.stale_symbols.add(symbol)
                logger.warning(
                    "posizione_senza_barre",
                    symbol=symbol,
                    quantity=quantity,
                    ultima_barra=None if barre.empty else str(barre.index[-1].date()),
                    giorni_di_ritardo=ritardo,
                )
            elif not ferma:
                self.stale_symbols.discard(symbol)

    def _barra_corrente(self, symbol: str, event: MarketEvent) -> pd.Series | None:
        """Barra del simbolo se scambia proprio oggi, None altrimenti."""
        barre = self.data_handler.get_latest_bars(symbol, 1)
        if barre.empty or barre.index[-1] != pd.Timestamp(event.timestamp):
            return None
        return barre.iloc[-1]

    def _quantita_precedente(self, symbol: str, event: MarketEvent) -> int:
        """Posizione come era alla chiusura precedente, al netto degli eseguiti di oggi."""
        oggi = pd.Timestamp(event.timestamp)
        variazione = 0
        for fill in reversed(self.fills):
            if pd.Timestamp(fill.timestamp) != oggi:
                break
            if fill.symbol == symbol:
                variazione += fill.quantity if fill.direction is OrderDirection.BUY else -fill.quantity
        return self.positions.get(symbol, 0) - variazione

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
