"""Stato live persistito su SQLite: ordini, fill, posizioni attese, equity."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from quant.events import FillEvent, OrderDirection, OrderEvent
from quant.logging import get_logger

PERCORSO_DB = Path("data/live.db")
MISMATCH = "RECONCILIATION_MISMATCH"
ESEGUITO = "filled"
STATI_CONCLUSI = (ESEGUITO, "canceled", "expired", "rejected")
logger = get_logger("state")

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    broker_order_id TEXT,
    run_id TEXT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    limit_price REAL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    client_order_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    fill_price REAL NOT NULL,
    commission REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    quantity INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equity (
    day TEXT PRIMARY KEY,
    cash REAL NOT NULL,
    positions_value REAL NOT NULL,
    total REAL NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class PositionMismatch:
    """Differenza fra la posizione attesa e quella riportata dal broker."""

    symbol: str
    expected: int
    actual: int

    @property
    def delta(self) -> int:
        """Scarto fra reale e atteso."""
        return self.actual - self.expected


@dataclass(frozen=True, slots=True)
class StoredOrder:
    """Riga della tabella ordini, come serve agli script di controllo."""

    client_order_id: str
    broker_order_id: str | None
    timestamp: str
    symbol: str
    direction: str
    quantity: int
    order_type: str
    limit_price: float | None
    status: str


@dataclass(frozen=True, slots=True)
class StoredFill:
    """Riga della tabella eseguiti, con l'identificativo dell'ordine che l'ha generata."""

    client_order_id: str
    timestamp: str
    symbol: str
    direction: str
    quantity: int
    fill_price: float
    commission: float


class StateStore:
    """Persistenza dello stato live: sopravvive al riavvio del processo."""

    def __init__(self, path: str | Path = PERCORSO_DB) -> None:
        self.path = Path(path)
        if self.path.parent != Path(""):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)

    @classmethod
    def read_only(cls, path: str | Path = PERCORSO_DB) -> StateStore:
        """Apre un database esistente in sola lettura, imposta dal driver SQLite.

        Non crea cartelle, file ne' tabelle: ogni scrittura sulla connessione solleva
        `sqlite3.OperationalError`. Chi mostra lo stato non deve poterlo cambiare.
        """
        percorso = Path(path)
        if not percorso.is_file():
            raise FileNotFoundError(percorso)
        store = cls.__new__(cls)
        store.path = percorso
        store.connection = sqlite3.connect(
            f"{percorso.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None
        )
        store.connection.row_factory = sqlite3.Row
        return store

    def close(self) -> None:
        """Chiude la connessione."""
        self.connection.close()

    def record_order(
        self,
        client_order_id: str,
        order: OrderEvent,
        order_type: str = "market",
        limit_price: float | None = None,
        status: str = "sent",
        broker_order_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """Registra un ordine inviato; la chiave primaria rende l'inserimento idempotente."""
        self.connection.execute(
            """INSERT INTO orders
               (client_order_id, broker_order_id, run_id, timestamp, symbol, direction, quantity,
                order_type, limit_price, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(client_order_id) DO UPDATE SET
                   broker_order_id = COALESCE(excluded.broker_order_id, orders.broker_order_id),
                   status = excluded.status""",
            (
                client_order_id,
                broker_order_id,
                run_id,
                order.timestamp.isoformat(),
                order.symbol,
                str(order.direction),
                int(order.quantity),
                order_type,
                limit_price,
                status,
            ),
        )

    def order_exists(self, client_order_id: str) -> bool:
        """True se quell'identificativo e' gia' stato usato."""
        riga = self.connection.execute(
            "SELECT 1 FROM orders WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()
        return riga is not None

    def update_order_status(
        self, client_order_id: str, status: str, broker_order_id: str | None = None
    ) -> None:
        """Aggiorna lo stato di un ordine gia' registrato."""
        self.connection.execute(
            """UPDATE orders SET status = ?,
                   broker_order_id = COALESCE(?, broker_order_id)
               WHERE client_order_id = ?""",
            (status, broker_order_id, client_order_id),
        )

    def recent_orders(self, limit: int = 10) -> list[StoredOrder]:
        """Ultimi ordini registrati, dal piu' recente."""
        return self.find_orders(limit=limit)

    def find_orders(
        self, start: date | None = None, end: date | None = None, symbol: str | None = None, limit: int = 500
    ) -> list[StoredOrder]:
        """Ordini registrati fra due giornate comprese e per un simbolo, dal piu' recente."""
        where, parametri = _filtro(start, end, symbol)
        righe = self.connection.execute(
            f"SELECT * FROM orders{where} ORDER BY timestamp DESC, rowid DESC LIMIT ?", (*parametri, limit)
        ).fetchall()
        return [
            StoredOrder(
                client_order_id=r["client_order_id"],
                broker_order_id=r["broker_order_id"],
                timestamp=r["timestamp"],
                symbol=r["symbol"],
                direction=r["direction"],
                quantity=r["quantity"],
                order_type=r["order_type"],
                limit_price=r["limit_price"],
                status=r["status"],
            )
            for r in righe
        ]

    def recent_fills(self, limit: int = 10) -> list[StoredFill]:
        """Ultimi eseguiti registrati, dal piu' recente."""
        return self.find_fills(limit=limit)

    def find_fills(
        self, start: date | None = None, end: date | None = None, symbol: str | None = None, limit: int = 500
    ) -> list[StoredFill]:
        """Eseguiti registrati fra due giornate comprese e per un simbolo, dal piu' recente."""
        where, parametri = _filtro(start, end, symbol)
        righe = self.connection.execute(
            f"SELECT * FROM fills{where} ORDER BY timestamp DESC, rowid DESC LIMIT ?", (*parametri, limit)
        ).fetchall()
        return [
            StoredFill(
                client_order_id=r["client_order_id"],
                timestamp=r["timestamp"],
                symbol=r["symbol"],
                direction=r["direction"],
                quantity=r["quantity"],
                fill_price=r["fill_price"],
                commission=r["commission"],
            )
            for r in righe
        ]

    def pending_orders(self) -> list[tuple[str, OrderEvent]]:
        """Ordini inviati e non ancora conclusi, ricostruiti dal database.

        Serve dopo un riavvio: l'handler in memoria non sa piu' nulla degli ordini
        di ieri, ma i loro eseguiti devono comunque arrivare al portafoglio.
        """
        segnaposto = ", ".join("?" for _ in STATI_CONCLUSI)
        righe = self.connection.execute(
            f"""SELECT o.* FROM orders o
               LEFT JOIN fills f ON f.client_order_id = o.client_order_id
               WHERE f.client_order_id IS NULL
                 AND o.status NOT IN ({segnaposto})
               ORDER BY o.timestamp""",
            STATI_CONCLUSI,
        ).fetchall()
        return [
            (
                r["client_order_id"],
                OrderEvent(
                    timestamp=datetime.fromisoformat(r["timestamp"]),
                    symbol=r["symbol"],
                    direction=OrderDirection(r["direction"]),
                    quantity=r["quantity"],
                ),
            )
            for r in righe
        ]

    def record_fill(self, client_order_id: str, fill: FillEvent) -> bool:
        """Registra un eseguito. False se era gia' presente, cosi' non si conta due volte."""
        if self.fill_exists(client_order_id):
            return False
        self.connection.execute(
            """INSERT INTO fills
               (client_order_id, timestamp, symbol, direction, quantity, fill_price, commission)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                client_order_id,
                fill.timestamp.isoformat(),
                fill.symbol,
                str(fill.direction),
                int(fill.quantity),
                float(fill.fill_price),
                float(fill.commission),
            ),
        )
        return True

    def fill_exists(self, client_order_id: str) -> bool:
        """True se l'eseguito di quell'ordine e' gia' stato registrato."""
        riga = self.connection.execute(
            "SELECT 1 FROM fills WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()
        return riga is not None

    def load_fills(self) -> list[FillEvent]:
        """Tutti gli eseguiti registrati, in ordine cronologico."""
        righe = self.connection.execute("SELECT * FROM fills ORDER BY timestamp, rowid").fetchall()
        return [
            FillEvent(
                timestamp=datetime.fromisoformat(r["timestamp"]),
                symbol=r["symbol"],
                direction=OrderDirection(r["direction"]),
                quantity=r["quantity"],
                fill_price=r["fill_price"],
                commission=r["commission"],
            )
            for r in righe
        ]

    def save_positions(self, positions: Mapping[str, int], moment: datetime | None = None) -> None:
        """Sovrascrive le posizioni attese."""
        adesso = (moment or datetime.now()).isoformat()
        self.connection.execute("DELETE FROM positions")
        self.connection.executemany(
            "INSERT INTO positions (symbol, quantity, updated_at) VALUES (?, ?, ?)",
            [(symbol, int(q), adesso) for symbol, q in positions.items() if q != 0],
        )

    def load_positions(self) -> dict[str, int]:
        """Posizioni attese salvate dall'ultima esecuzione."""
        righe = self.connection.execute("SELECT symbol, quantity FROM positions").fetchall()
        return {r["symbol"]: r["quantity"] for r in righe}

    def save_equity(self, giorno: date, cash: float, positions_value: float) -> None:
        """Registra l'equity di una giornata."""
        self.connection.execute(
            """INSERT INTO equity (day, cash, positions_value, total) VALUES (?, ?, ?, ?)
               ON CONFLICT(day) DO UPDATE SET
                   cash = excluded.cash,
                   positions_value = excluded.positions_value,
                   total = excluded.total""",
            (giorno.isoformat(), float(cash), float(positions_value), float(cash + positions_value)),
        )

    def load_equity(self, days: int = 30) -> list[tuple[date, float, float, float]]:
        """Ultime giornate di equity, dalla piu' vecchia alla piu' recente."""
        righe = self.connection.execute(
            "SELECT * FROM equity ORDER BY day DESC LIMIT ?", (days,)
        ).fetchall()
        return [
            (date.fromisoformat(r["day"]), r["cash"], r["positions_value"], r["total"])
            for r in reversed(righe)
        ]


def reconcile(expected: Mapping[str, int], actual: Mapping[str, int]) -> list[PositionMismatch]:
    """Confronta posizioni attese e reali e logga ogni differenza.

    Non corregge nulla: una correzione automatica su uno stato che non si capisce
    e' il modo piu' rapido per trasformare un disallineamento in una perdita.
    """
    simboli = sorted(set(expected) | set(actual))
    differenze = [
        PositionMismatch(symbol, int(expected.get(symbol, 0)), int(actual.get(symbol, 0)))
        for symbol in simboli
        if int(expected.get(symbol, 0)) != int(actual.get(symbol, 0))
    ]
    for differenza in differenze:
        logger.error(
            MISMATCH,
            symbol=differenza.symbol,
            expected=differenza.expected,
            actual=differenza.actual,
            delta=differenza.delta,
        )
    return differenze


def _filtro(start: date | None, end: date | None, symbol: str | None) -> tuple[str, tuple[object, ...]]:
    """Clausola WHERE per giornata e simbolo: i timestamp ISO si confrontano come testo."""
    condizioni: list[str] = []
    parametri: list[object] = []
    if start is not None:
        condizioni.append("timestamp >= ?")
        parametri.append(start.isoformat())
    if end is not None:
        condizioni.append("timestamp < ?")
        parametri.append((end + timedelta(days=1)).isoformat())
    if symbol:
        condizioni.append("symbol = ?")
        parametri.append(symbol)
    return (" WHERE " + " AND ".join(condizioni) if condizioni else ""), tuple(parametri)
