"""Test del DataHandler: cursore, barre mancanti, assenza di look-ahead."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from quant.data import ParquetDataHandler

SYMBOLS = ["SPY", "QQQ", "TLT", "GLD"]


def test_timeline_e_unione_delle_date(parquet_dir: Path) -> None:
    handler = ParquetDataHandler(parquet_dir, SYMBOLS)
    assert len(handler.timeline) == 20


def test_get_latest_bars_non_supera_il_cursore(parquet_dir: Path) -> None:
    handler = ParquetDataHandler(parquet_dir, SYMBOLS)
    for _ in range(5):
        handler.update_bars()
    current = handler.current_timestamp()
    bars = handler.get_latest_bars("SPY", 3)
    assert len(bars) == 3
    assert bars.index.max() == pd.Timestamp(current)


def test_n_maggiore_delle_barre_disponibili(parquet_dir: Path) -> None:
    handler = ParquetDataHandler(parquet_dir, SYMBOLS)
    handler.update_bars()
    handler.update_bars()
    bars = handler.get_latest_bars("SPY", 500)
    assert len(bars) == 2
    assert bars.index.max() == pd.Timestamp(handler.current_timestamp())


def test_nessuna_barra_prima_del_primo_update(parquet_dir: Path) -> None:
    handler = ParquetDataHandler(parquet_dir, SYMBOLS)
    assert handler.current_timestamp() is None
    assert handler.get_latest_bars("SPY", 5).empty


def test_simbolo_con_barre_mancanti_non_e_forward_filled(parquet_dir: Path) -> None:
    handler = ParquetDataHandler(parquet_dir, SYMBOLS)
    for _ in range(4):
        handler.update_bars()
    assert not handler.has_bar("TLT")
    assert handler.has_bar("SPY")
    tlt = handler.get_latest_bars("TLT", 1)
    assert len(tlt) == 1
    assert tlt.index[-1] < pd.Timestamp(handler.current_timestamp())
    assert len(handler.get_latest_bars("TLT", 4)) == 3


def test_filtro_start_end(parquet_dir: Path) -> None:
    handler = ParquetDataHandler(parquet_dir, SYMBOLS, start="2020-01-06", end="2020-01-10")
    assert len(handler.timeline) == 5


def test_get_latest_bars_all_copre_tutti_i_simboli(parquet_dir: Path) -> None:
    handler = ParquetDataHandler(parquet_dir, SYMBOLS)
    for _ in range(6):
        handler.update_bars()
    tutte = handler.get_latest_bars_all(3)
    assert set(tutte) == set(SYMBOLS)
    assert all(len(frame) == 3 for frame in tutte.values())


def test_get_latest_bars_all_non_supera_il_cursore(parquet_dir: Path) -> None:
    handler = ParquetDataHandler(parquet_dir, SYMBOLS)
    while handler.continue_backtest:
        handler.update_bars()
        limite = pd.Timestamp(handler.current_timestamp())
        for symbol, frame in handler.get_latest_bars_all(50).items():
            assert frame.index.max() <= limite, symbol


def test_get_latest_bars_all_rispetta_le_barre_mancanti(parquet_dir: Path) -> None:
    handler = ParquetDataHandler(parquet_dir, SYMBOLS)
    for _ in range(4):
        handler.update_bars()
    tutte = handler.get_latest_bars_all(4)
    assert len(tutte["SPY"]) == 4
    assert len(tutte["TLT"]) == 3
    assert tutte["TLT"].index.max() < pd.Timestamp(handler.current_timestamp())


def test_emitted_timeline_cresce_con_il_cursore(parquet_dir: Path) -> None:
    handler = ParquetDataHandler(parquet_dir, SYMBOLS)
    assert len(handler.emitted_timeline()) == 0
    for atteso in range(1, 6):
        handler.update_bars()
        emesse = handler.emitted_timeline()
        assert len(emesse) == atteso
        assert emesse.max() == pd.Timestamp(handler.current_timestamp())
