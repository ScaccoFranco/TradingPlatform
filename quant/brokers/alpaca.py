"""Esecuzione su Alpaca: stessa interfaccia dell'esecuzione simulata del backtest."""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any

from quant.config import Settings, load_settings
from quant.events import FillEvent, MarketEvent, OrderDirection, OrderEvent
from quant.execution import ExecutionHandler
from quant.logging import get_logger
from quant.state import StateStore

logger = get_logger("alpaca.execution")

STATI_CHIUSI = {"filled", "canceled", "expired", "rejected", "done_for_day"}


def client_order_id(order_date: date, symbol: str, strategy: str, prefix: str = "quant") -> str:
    """Identificativo deterministico: stessa giornata, simbolo e strategia danno la stessa stringa.

    E' la difesa contro i doppioni: se il processo riparte e ricalcola lo stesso
    ordine, il broker lo riconosce come gia' visto invece di eseguirlo due volte.
    Come conseguenza voluta, per una strategia c'e' al massimo un ordine al giorno
    per simbolo.
    """
    grezzo = f"{strategy}|{symbol}|{order_date.isoformat()}"
    return f"{prefix}-{hashlib.sha256(grezzo.encode('utf-8')).hexdigest()[:16]}"


class AlpacaExecutionHandler(ExecutionHandler):
    """Invia ordini reali al paper trading, con idempotenza e stato persistito.

    Il client viene iniettato: i test passano un finto, la produzione quello vero
    costruito da `from_settings`. Nessuna chiamata di rete nel costruttore.
    """

    def __init__(
        self,
        client: Any,
        strategy_name: str = "quant",
        state_store: StateStore | None = None,
        order_type: str = "market",
        limit_offset_bps: float = 0.0,
        data_handler: Any = None,
        time_in_force: str = "day",
        run_id: str | None = None,
    ) -> None:
        self.client = client
        self.strategy_name = strategy_name
        self.state_store = state_store
        self.order_type = order_type
        self.limit_offset_bps = float(limit_offset_bps)
        self.data_handler = data_handler
        self.time_in_force = time_in_force
        self.run_id = run_id
        self.submitted: dict[str, OrderEvent] = {}

    @classmethod
    def from_settings(cls, settings: Settings | None = None, **kwargs: Any) -> AlpacaExecutionHandler:
        """Costruisce il client vero dopo aver verificato che si tratti di paper trading."""
        from alpaca.trading.client import TradingClient

        impostazioni = (settings or load_settings()).validate_live()
        client = TradingClient(
            api_key=impostazioni.alpaca_api_key,
            secret_key=impostazioni.alpaca_secret_key,
            paper=impostazioni.alpaca_paper,
        )
        return cls(client, **kwargs)

    def on_order(self, order: OrderEvent) -> None:
        """Interfaccia del motore: inoltra l'ordine al broker."""
        self.submit_order(order)

    def on_market(self, event: MarketEvent) -> list[FillEvent]:
        """Interfaccia del motore: raccoglie gli eseguiti degli ordini gia' inviati."""
        return self.collect_fills()

    def submit_order(self, order: OrderEvent, limit_price: float | None = None) -> Any | None:
        """Invia un ordine market o limit; restituisce None se e' un doppione."""
        identificativo = client_order_id(order.timestamp.date(), order.symbol, self.strategy_name)
        if self._gia_inviato(identificativo):
            logger.warning(
                "ordine_duplicato_ignorato",
                client_order_id=identificativo,
                symbol=order.symbol,
                quantity=order.quantity,
            )
            return None

        prezzo = limit_price if limit_price is not None else self._limit_price(order)
        richiesta = self._costruisci_richiesta(order, identificativo, prezzo)
        if self.state_store is not None:
            self.state_store.record_order(
                identificativo,
                order,
                order_type="limit" if prezzo is not None else "market",
                limit_price=prezzo,
                status="sent",
                run_id=self.run_id,
            )
        risposta = self.client.submit_order(order_data=richiesta)
        self.submitted[identificativo] = order
        broker_id = str(getattr(risposta, "id", "")) or None
        if self.state_store is not None:
            self.state_store.update_order_status(identificativo, "submitted", broker_id)
        logger.info(
            "ordine_inviato",
            client_order_id=identificativo,
            broker_order_id=broker_id,
            symbol=order.symbol,
            direction=str(order.direction),
            quantity=order.quantity,
            limit_price=prezzo,
        )
        return risposta

    def cancel_order(self, broker_order_id: str) -> None:
        """Annulla un ordine ancora aperto presso il broker."""
        self.client.cancel_order_by_id(broker_order_id)
        logger.warning("ordine_annullato", broker_order_id=broker_order_id)

    def get_positions(self) -> dict[str, int]:
        """Posizioni reali sul conto, per simbolo."""
        posizioni = {}
        for posizione in self.client.get_all_positions():
            posizioni[posizione.symbol] = int(float(posizione.qty))
        return posizioni

    def get_cash(self) -> float:
        """Liquidita' disponibile sul conto."""
        return float(self.client.get_account().cash)

    def get_open_orders(self) -> list[Any]:
        """Ordini ancora aperti presso il broker."""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        return list(self.client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)))

    def collect_fills(self) -> list[FillEvent]:
        """Trasforma in FillEvent gli ordini inviati che risultano eseguiti.

        Gli ordini da controllare arrivano dalla memoria e dallo store: lo scheduler
        ricrea l'handler a ogni esecuzione, quindi senza il database gli eseguiti del
        giorno prima non verrebbero mai raccolti. Un eseguito gia' registrato non viene
        restituito due volte: il portafoglio non deve contare la stessa operazione due volte.
        """
        fills: list[FillEvent] = []
        for identificativo, ordine in self._da_controllare().items():
            stato = self._stato_ordine(identificativo)
            if stato is None:
                continue
            eseguito = self._to_fill(stato, ordine)
            if eseguito is None:
                self._chiudi_se_concluso(identificativo, stato)
                continue
            if self.state_store is not None and not self.state_store.record_fill(identificativo, eseguito):
                self.submitted.pop(identificativo, None)
                continue
            if self.state_store is not None:
                self.state_store.update_order_status(identificativo, "filled")
            self.submitted.pop(identificativo, None)
            fills.append(eseguito)
            logger.info(
                "fill_ricevuto",
                client_order_id=identificativo,
                symbol=eseguito.symbol,
                quantity=eseguito.quantity,
                fill_price=eseguito.fill_price,
            )
        return fills

    def _da_controllare(self) -> dict[str, OrderEvent]:
        """Ordini di cui aspettiamo l'esito, dalla memoria e dallo stato persistito."""
        ordini = dict(self.submitted)
        if self.state_store is not None:
            for identificativo, ordine in self.state_store.pending_orders():
                ordini.setdefault(identificativo, ordine)
        return ordini

    def _chiudi_se_concluso(self, identificativo: str, stato: Any) -> None:
        """Segna come chiusi gli ordini che il broker ha annullato, respinto o fatto scadere."""
        etichetta = str(getattr(stato, "status", "")).lower().rsplit(".", 1)[-1]
        if etichetta in STATI_CHIUSI and etichetta != "filled":
            self.submitted.pop(identificativo, None)
            if self.state_store is not None:
                self.state_store.update_order_status(identificativo, etichetta)
            logger.warning("ordine_concluso_senza_fill", client_order_id=identificativo, status=etichetta)

    def _gia_inviato(self, identificativo: str) -> bool:
        """Controlla lo store e poi il broker, senza fidarsi della sola memoria."""
        if identificativo in self.submitted:
            return True
        if self.state_store is not None and self.state_store.order_exists(identificativo):
            return True
        return self._stato_ordine(identificativo) is not None

    def _stato_ordine(self, identificativo: str) -> Any | None:
        """Ordine come lo conosce il broker, None se non lo conosce."""
        try:
            return self.client.get_order_by_client_id(identificativo)
        except Exception:  # noqa: BLE001 - l'SDK alza eccezioni diverse per "non trovato"
            return None

    def _to_fill(self, stato: Any, ordine: OrderEvent) -> FillEvent | None:
        """Converte un ordine eseguito in FillEvent, None se non e' ancora eseguito."""
        if str(getattr(stato, "status", "")).lower().rsplit(".", 1)[-1] != "filled":
            return None
        quantita = int(float(getattr(stato, "filled_qty", 0) or 0))
        prezzo = float(getattr(stato, "filled_avg_price", 0) or 0)
        if quantita <= 0 or prezzo <= 0:
            return None
        momento = getattr(stato, "filled_at", None) or ordine.timestamp
        return FillEvent(
            timestamp=momento if isinstance(momento, datetime) else ordine.timestamp,
            symbol=ordine.symbol,
            direction=ordine.direction,
            quantity=quantita,
            fill_price=prezzo,
            commission=0.0,
        )

    def _limit_price(self, order: OrderEvent) -> float | None:
        """Prezzo limite ricavato dall'ultimo close, a favore dell'esecuzione."""
        if self.order_type != "limit" or self.data_handler is None:
            return None
        barre = self.data_handler.get_latest_bars(order.symbol, 1)
        if barre.empty:
            logger.warning("limite_non_calcolabile", symbol=order.symbol)
            return None
        riferimento = float(barre["close"].iloc[-1])
        scarto = self.limit_offset_bps / 10_000.0
        if order.direction is OrderDirection.BUY:
            return round(riferimento * (1.0 + scarto), 2)
        return round(riferimento * (1.0 - scarto), 2)

    def _costruisci_richiesta(self, order: OrderEvent, identificativo: str, limit_price: float | None) -> Any:
        """Richiesta market o limit nel formato dell'SDK."""
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        comune = {
            "symbol": order.symbol,
            "qty": order.quantity,
            "side": OrderSide.BUY if order.direction is OrderDirection.BUY else OrderSide.SELL,
            "time_in_force": TimeInForce(self.time_in_force),
            "client_order_id": identificativo,
        }
        if limit_price is not None:
            return LimitOrderRequest(limit_price=limit_price, **comune)
        return MarketOrderRequest(**comune)
