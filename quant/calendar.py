"""Rilevamento del cambio mese senza look-ahead.

Scelta di progetto: il ribilanciamento avviene sulla PRIMA barra di ogni mese.
Sapere se la barra corrente e' l'ULTIMA del mese richiederebbe di conoscere il
calendario futuro (festivita' incluse), informazione non disponibile in live;
sapere se e' la PRIMA e' invece decidibile guardando solo la barra precedente,
che e' gia' stata emessa. Le strategie usano quindi
`is_first_trading_day_of_month`; `is_last_trading_day_of_month` resta disponibile
per analisi su dataset completi ed e' esplicitamente marcata come non usabile
come trigger operativo.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from quant.data import DataHandler


def is_first_trading_day_of_month(timestamp: datetime, data_handler: DataHandler) -> bool:
    """True se `timestamp` e' la prima barra del suo mese fra quelle gia' emesse.

    Usa solo `emitted_timeline()`: la barra precedente e' passato, nessuna data
    futura viene consultata. La primissima barra del backtest conta come inizio mese.
    """
    emitted = data_handler.emitted_timeline()
    current = pd.Timestamp(timestamp)
    position = int(emitted.searchsorted(current, side="left"))
    if position == 0:
        return True
    previous = emitted[position - 1]
    return (previous.year, previous.month) != (current.year, current.month)


def is_last_trading_day_of_month(timestamp: datetime, data_handler: DataHandler | None = None) -> bool:
    """True se `timestamp` e' l'ultimo giorno feriale del mese sul calendario weekday.

    Attenzione: e' un'approssimazione. Il vero ultimo giorno di contrattazione
    dipende dalle festivita' future, quindi non e' decidibile senza look-ahead e
    questa funzione restituisce False sull'ultimo giorno scambiato quando i
    weekday successivi del mese sono festivi. Per il ribilanciamento usare
    `is_first_trading_day_of_month`.
    """
    current = pd.Timestamp(timestamp)
    next_business_day = current + pd.offsets.BDay(1)
    return (next_business_day.year, next_business_day.month) != (current.year, current.month)


def month_key(timestamp: datetime) -> tuple[int, int]:
    """Coppia (anno, mese) usata per confrontare due barre."""
    current = pd.Timestamp(timestamp)
    return (current.year, current.month)
