"""Client Alpaca finti: nessuna rete, stesso comportamento che serve ai test."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pandas as pd


class OrdineFinto:
    """Ordine come lo restituisce il broker."""

    def __init__(self, identificativo: str, richiesta: Any) -> None:
        self.id = identificativo
        self.client_order_id = richiesta.client_order_id
        self.symbol = richiesta.symbol
        self.qty = richiesta.qty
        self.side = richiesta.side
        self.status = "accepted"
        self.filled_qty = 0
        self.filled_avg_price = None
        self.filled_at: datetime | None = None

    def esegui(self, prezzo: float, quando: datetime, quantita: int | None = None) -> None:
        """Simula l'esecuzione totale o parziale dell'ordine."""
        self.status = "filled"
        self.filled_qty = quantita if quantita is not None else int(self.qty)
        self.filled_avg_price = prezzo
        self.filled_at = quando


class TradingClientFinto:
    """Sostituto di `alpaca.trading.client.TradingClient` per i test."""

    def __init__(self, cash: float = 100_000.0, positions: dict[str, int] | None = None) -> None:
        self.cash = cash
        self._positions = dict(positions or {})
        self.per_client_id: dict[str, OrdineFinto] = {}
        self.inviati: list[Any] = []
        self.annullati: list[str] = []
        self.contatore = 0

    def submit_order(self, order_data: Any) -> OrdineFinto:
        """Registra l'ordine; un client_order_id ripetuto e' un errore, come sul broker vero."""
        if order_data.client_order_id in self.per_client_id:
            raise RuntimeError("client_order_id gia' usato")
        self.contatore += 1
        ordine = OrdineFinto(f"broker-{self.contatore}", order_data)
        self.per_client_id[order_data.client_order_id] = ordine
        self.inviati.append(order_data)
        return ordine

    def get_order_by_client_id(self, client_order_id: str) -> OrdineFinto:
        if client_order_id not in self.per_client_id:
            raise RuntimeError("ordine non trovato")
        return self.per_client_id[client_order_id]

    def get_all_positions(self) -> list[Any]:
        return [SimpleNamespace(symbol=s, qty=str(q)) for s, q in self._positions.items()]

    def get_account(self) -> Any:
        return SimpleNamespace(cash=str(self.cash))

    def get_orders(self, filter: Any = None) -> list[OrdineFinto]:  # noqa: A002 - firma dell'SDK
        return [o for o in self.per_client_id.values() if o.status not in {"filled", "canceled"}]

    def cancel_order_by_id(self, broker_order_id: str) -> None:
        self.annullati.append(broker_order_id)
        for ordine in self.per_client_id.values():
            if ordine.id == broker_order_id:
                ordine.status = "canceled"


class DataClientFinto:
    """Sostituto del client storico: restituisce un DataFrame nel formato dell'SDK."""

    def __init__(self, barre: dict[str, pd.DataFrame], fattore_rettifica: float = 1.0) -> None:
        self.barre = barre
        self.fattore_rettifica = fattore_rettifica
        self.richieste: list[Any] = []

    def get_stock_bars(self, request: Any) -> Any:
        """Risposta con l'attributo `.df` a indice multiplo (symbol, timestamp)."""
        self.richieste.append(request)
        pezzi = []
        adjustment = getattr(request, "adjustment", "raw")
        rettificato = str(getattr(adjustment, "value", adjustment)) == "all"
        for symbol, frame in self.barre.items():
            copia = frame.copy()
            if rettificato:
                copia["close"] = copia["close"] * self.fattore_rettifica
            copia["symbol"] = symbol
            copia["timestamp"] = pd.to_datetime(copia.index, utc=True)
            pezzi.append(copia.reset_index(drop=True))
        unione = pd.concat(pezzi, ignore_index=True)
        return SimpleNamespace(df=unione.set_index(["symbol", "timestamp"]))
