"""Test property-based: nessuna barra oltre il cursore, su calendari generati a caso."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from quant.data import COLUMNS, ParquetDataHandler

SIMBOLI = ["AAA", "BBB", "CCC"]
IMPOSTAZIONI = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


def scrivi(directory: Path, symbol: str, date: pd.DatetimeIndex) -> None:
    """Parquet sintetico con le colonne di sistema e prezzi crescenti."""
    valori = [100.0 + i for i in range(len(date))]
    frame = pd.DataFrame(
        {
            "open": valori,
            "high": [v + 1 for v in valori],
            "low": [v - 1 for v in valori],
            "close": valori,
            "adj_close": valori,
            "volume": [1_000.0] * len(date),
            "dividends": [0.0] * len(date),
            "split_factor": [1.0] * len(date),
        },
        index=date,
    )
    frame.index.name = "date"
    frame[list(COLUMNS)].to_parquet(directory / f"{symbol}.parquet")


@st.composite
def universo(disegna: st.DrawFn) -> tuple[list[pd.DatetimeIndex], int]:
    """Un calendario per simbolo con buchi casuali, piu' un numero di barre da consumare."""
    lunghezza = disegna(st.integers(min_value=2, max_value=60))
    inizio = disegna(
        st.dates(min_value=pd.Timestamp("2000-01-03").date(), max_value=pd.Timestamp("2020-01-01").date())
    )
    calendario = pd.bdate_range(pd.Timestamp(inizio), periods=lunghezza)

    calendari = []
    for _ in SIMBOLI:
        tenute = disegna(st.lists(st.booleans(), min_size=lunghezza, max_size=lunghezza))
        scelte = [d for d, tieni in zip(calendario, tenute, strict=False) if tieni] or [calendario[0]]
        calendari.append(pd.DatetimeIndex(scelte))
    passi = disegna(st.integers(min_value=0, max_value=lunghezza + 5))
    return calendari, passi


@given(dati=universo(), n=st.integers(min_value=1, max_value=90))
@IMPOSTAZIONI
def test_nessuna_barra_oltre_il_cursore(tmp_path_factory, dati, n) -> None:
    """Ne' per simbolo ne' in blocco il cursore puo' essere superato."""
    calendari, passi = dati
    directory = tmp_path_factory.mktemp("lookahead")
    for symbol, calendario in zip(SIMBOLI, calendari, strict=False):
        scrivi(directory, symbol, calendario)

    handler = ParquetDataHandler(directory, SIMBOLI)
    for _ in range(passi):
        handler.update_bars()

    corrente = handler.current_timestamp()
    if corrente is None:
        assert all(handler.get_latest_bars(s, n).empty for s in SIMBOLI)
        assert all(f.empty for f in handler.get_latest_bars_all(n).values())
        return

    limite = pd.Timestamp(corrente)
    for symbol in SIMBOLI:
        barre = handler.get_latest_bars(symbol, n)
        assert len(barre) <= n
        assert barre.empty or barre.index.max() <= limite
    for symbol, barre in handler.get_latest_bars_all(n).items():
        assert barre.empty or barre.index.max() <= limite, symbol


@given(dati=universo())
@IMPOSTAZIONI
def test_le_date_emesse_sono_un_prefisso_della_timeline(tmp_path_factory, dati) -> None:
    """`emitted_timeline` non puo' saltare avanti ne' scoprire date fuori ordine."""
    calendari, passi = dati
    directory = tmp_path_factory.mktemp("prefisso")
    for symbol, calendario in zip(SIMBOLI, calendari, strict=False):
        scrivi(directory, symbol, calendario)

    handler = ParquetDataHandler(directory, SIMBOLI)
    for _ in range(passi):
        handler.update_bars()
        emesse = handler.emitted_timeline()
        assert list(emesse) == list(handler.timeline[: len(emesse)])
        assert len(emesse) <= len(handler.timeline)
        if len(emesse):
            assert emesse[-1] == pd.Timestamp(handler.current_timestamp())


@given(dati=universo(), n=st.integers(min_value=1, max_value=10))
@IMPOSTAZIONI
def test_le_barre_restituite_sono_le_ultime_disponibili(tmp_path_factory, dati, n) -> None:
    """Le barre visibili coincidono con la coda della serie troncata al cursore."""
    calendari, passi = dati
    directory = tmp_path_factory.mktemp("coda")
    for symbol, calendario in zip(SIMBOLI, calendari, strict=False):
        scrivi(directory, symbol, calendario)

    handler = ParquetDataHandler(directory, SIMBOLI)
    for _ in range(passi):
        handler.update_bars()
    corrente = handler.current_timestamp()
    if corrente is None:
        return

    for symbol in SIMBOLI:
        completo = pd.read_parquet(directory / f"{symbol}.parquet")
        atteso = completo[completo.index <= pd.Timestamp(corrente)].tail(n)
        ottenuto = handler.get_latest_bars(symbol, n)
        assert list(ottenuto.index) == list(atteso.index)
