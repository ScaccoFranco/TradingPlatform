"""Barre sintetiche, sorgente e alerter finti, condivisi dai test della catena dati."""

from __future__ import annotations

from datetime import date

import pandas as pd

from quant.sources import DataSource, DataSourceError, Giorno


def adj_coerente(frame: pd.DataFrame) -> pd.Series:
    """adj_close a fattore cumulativo all'indietro, calcolato a mano barra per barra."""
    close, cedole, split = list(frame["close"]), list(frame["dividend"]), list(frame["split"])
    fattore, adj = 1.0, [0.0] * len(close)
    for i in reversed(range(len(close))):
        adj[i] = close[i] * fattore
        if i > 0:
            fattore *= (1.0 - cedole[i] * split[i] / close[i - 1]) / split[i]
    return pd.Series(adj, index=frame.index)


def barre_sorgente(
    date_: list[str] | pd.DatetimeIndex,
    cedole: dict[str, float] | None = None,
    split: dict[str, float] | None = None,
    base: float = 100.0,
    passo: float = 1.0,
) -> pd.DataFrame:
    """Valore economico che cresce di `passo` al giorno, prezzi grezzi divisi dagli split.

    `adj_close` e' coerente con cedole e split, come la consegnerebbe un vendor corretto.
    """
    indice = pd.DatetimeIndex(date_, name="date")
    fattori = pd.Series(1.0, index=indice)
    for giorno, valore in (split or {}).items():
        fattori[pd.Timestamp(giorno)] = valore
    scala = fattori.cumprod()
    close = [(base + passo * i) / s for i, s in enumerate(scala)]
    frame = pd.DataFrame(
        {
            "open": [c - 0.25 for c in close],
            "high": [c + 1.0 for c in close],
            "low": [c - 1.0 for c in close],
            "close": close,
            "volume": [1_000_000.0 * s for s in scala],
            "dividend": 0.0,
            "split": fattori,
        },
        index=indice,
    )
    for giorno, valore in (cedole or {}).items():
        frame.loc[pd.Timestamp(giorno), "dividend"] = valore
    frame["adj_close"] = adj_coerente(frame)
    return frame.astype("float64")


class SorgenteFinta(DataSource):
    """Serve un DataFrame in memoria tagliato sull'intervallo richiesto, e annota le richieste."""

    name = "finta"

    def __init__(self, barre: dict[str, pd.DataFrame]) -> None:
        self.barre = barre
        self.richieste: list[tuple[str, date, date | None]] = []

    def fetch(self, symbol: str, start: Giorno, end: Giorno | None = None) -> pd.DataFrame:
        inizio = pd.Timestamp(start)
        fine = None if end is None else pd.Timestamp(end)
        self.richieste.append((symbol, inizio.date(), None if fine is None else fine.date()))
        if symbol not in self.barre:
            raise DataSourceError(f"{symbol}: simbolo sconosciuto")
        frame = self.barre[symbol]
        frame = frame[frame.index >= inizio]
        return frame if fine is None else frame[frame.index <= fine]


class AlerterFinto:
    def __init__(self) -> None:
        self.messaggi: list[tuple[str, str]] = []

    def send(self, level: str, message: str) -> bool:
        self.messaggi.append((level, message))
        return True
