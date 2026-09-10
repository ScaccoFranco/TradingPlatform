"""Test del rilevamento del cambio mese su calendario sintetico con festivita'."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from quant.calendar import is_first_trading_day_of_month, is_last_trading_day_of_month
from quant.data import ParquetDataHandler

SYMBOLS = ["SPY", "IEF", "GLD"]
PRIME_BARRE_DEL_MESE = ["2021-01-04", "2021-02-01", "2021-03-01", "2021-04-01", "2021-05-03", "2021-06-01"]


def raccogli_prime_barre(directory: Path) -> list[pd.Timestamp]:
    """Percorre il backtest e raccoglie le barre riconosciute come inizio mese."""
    handler = ParquetDataHandler(directory, SYMBOLS)
    trovate: list[pd.Timestamp] = []
    while handler.continue_backtest:
        for event in handler.update_bars():
            if is_first_trading_day_of_month(event.timestamp, handler):
                trovate.append(pd.Timestamp(event.timestamp))
    return trovate


def test_prima_barra_del_mese_salta_le_festivita(calendar_parquet_dir: Path) -> None:
    trovate = raccogli_prime_barre(calendar_parquet_dir)
    assert trovate == [pd.Timestamp(d) for d in PRIME_BARRE_DEL_MESE]


def test_una_sola_prima_barra_per_mese(calendar_parquet_dir: Path) -> None:
    trovate = raccogli_prime_barre(calendar_parquet_dir)
    mesi = [(t.year, t.month) for t in trovate]
    assert len(mesi) == len(set(mesi)) == 6


def test_decisione_identica_senza_dati_futuri(calendar_parquet_dir: Path) -> None:
    """La stessa barra decide allo stesso modo se il dataset finisce li'."""
    completo = ParquetDataHandler(calendar_parquet_dir, SYMBOLS)
    while completo.continue_backtest:
        for event in completo.update_bars():
            troncato = ParquetDataHandler(calendar_parquet_dir, SYMBOLS, end=event.timestamp)
            while troncato.continue_backtest:
                troncato.update_bars()
            assert is_first_trading_day_of_month(event.timestamp, completo) == is_first_trading_day_of_month(
                event.timestamp, troncato
            )


def test_ultima_barra_del_mese_e_approssimata_sul_calendario_feriale(calendar_parquet_dir: Path) -> None:
    """Il limite documentato: con l'ultimo feriale festivo la funzione non lo riconosce."""
    handler = ParquetDataHandler(calendar_parquet_dir, SYMBOLS)
    assert is_last_trading_day_of_month(pd.Timestamp("2021-01-29"), handler) is True
    assert is_last_trading_day_of_month(pd.Timestamp("2021-05-31"), handler) is True
    assert is_last_trading_day_of_month(pd.Timestamp("2021-05-28"), handler) is False
