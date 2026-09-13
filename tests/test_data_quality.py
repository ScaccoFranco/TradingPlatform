"""Controlli di qualita': ogni controllo scatta sul suo guasto sintetico e tace sul caso quasi uguale."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest
from sintetici import adj_coerente

from quant.config import DataQualityConfig, load_settings
from quant.data_quality import Finding, Severity, alpaca_calendar, build_calendar, check

SEDUTE = pd.bdate_range("2018-03-01", periods=30, name="date")
CEDOLA = pd.Timestamp("2018-03-15")
SPLIT = pd.Timestamp("2018-03-22")


def serie_pulita() -> pd.DataFrame:
    """Trenta sedute con una cedola e uno split 2:1, tutte coerenti: nessun finding."""
    close = [100.0 + 0.5 * i for i in range(len(SEDUTE))]
    frame = pd.DataFrame(
        {
            "open": close,
            "high": [c + 1.0 for c in close],
            "low": [c - 1.0 for c in close],
            "close": close,
            "volume": 1_000_000.0,
            "dividend": 0.0,
            "split": 1.0,
        },
        index=SEDUTE,
    )
    frame.loc[CEDOLA, "dividend"] = 0.8
    dopo = frame.index >= SPLIT
    frame.loc[dopo, ["open", "high", "low", "close"]] /= 2.0
    frame.loc[dopo, "volume"] *= 2.0
    frame.loc[SPLIT, "split"] = 2.0
    frame["adj_close"] = adj_coerente(frame)
    return frame


def imposta(giorno: str, **valori: float) -> Callable[[pd.DataFrame], pd.DataFrame]:
    def guasto(frame: pd.DataFrame) -> pd.DataFrame:
        for colonna, valore in valori.items():
            frame.loc[pd.Timestamp(giorno), colonna] = valore
        return frame

    return guasto


def duplica(giorno: str) -> Callable[[pd.DataFrame], pd.DataFrame]:
    return lambda f: pd.concat([f, f.loc[[pd.Timestamp(giorno)]]]).sort_index()


def scambia(i: int) -> Callable[[pd.DataFrame], pd.DataFrame]:
    return lambda f: f.iloc[[*range(i), i + 1, i, *range(i + 2, len(f))]]


def togli(giorno: str) -> Callable[[pd.DataFrame], pd.DataFrame]:
    return lambda f: f.drop(index=pd.Timestamp(giorno))


def salto(giorno: str, variazione: float) -> Callable[[pd.DataFrame], pd.DataFrame]:
    """Sposta close e adj_close della barra, lasciando la barra coerente con se stessa."""

    def guasto(f: pd.DataFrame) -> pd.DataFrame:
        g = pd.Timestamp(giorno)
        precedente = f["close"].shift(1)[g] / f.at[g, "split"]
        nuovo = precedente * (1.0 + variazione)
        f.loc[g, ["open", "close"]] = nuovo
        f.loc[g, "high"], f.loc[g, "low"] = nuovo + 1.0, nuovo - 1.0
        f["adj_close"] = adj_coerente(f)
        return f

    return guasto


def scala_adj(da: str, fattore: float) -> Callable[[pd.DataFrame], pd.DataFrame]:
    def guasto(f: pd.DataFrame) -> pd.DataFrame:
        f.loc[f.index >= pd.Timestamp(da), "adj_close"] *= fattore
        return f

    return guasto


def cedola_ignorata_dal_vendor(f: pd.DataFrame) -> pd.DataFrame:
    """Il vendor rettifica per la cedola del 15 marzo, i dati grezzi non la riportano."""
    f.loc[CEDOLA, "dividend"] = 0.0
    return f


def codici(findings: tuple[Finding, ...]) -> list[tuple[str, str, date | None]]:
    return [(f.severity.value, f.code, f.date) for f in findings]


def test_serie_pulita_senza_finding() -> None:
    assert check(serie_pulita(), "SPY", SEDUTE).findings == ()


GUASTI = [
    ("DUPLICATE_DATE", Severity.BLOCKING, "2018-03-06", duplica("2018-03-06")),
    ("UNSORTED_INDEX", Severity.BLOCKING, "2018-03-06", scambia(3)),
    ("MISSING_DAY", Severity.WARNING, "2018-03-08", togli("2018-03-08")),
    ("INVALID_PRICE", Severity.BLOCKING, "2018-03-08", imposta("2018-03-08", open=float("nan"))),
    ("INVALID_PRICE", Severity.BLOCKING, "2018-03-08", imposta("2018-03-08", low=0.0, high=0.0, open=0.0)),
    ("INVALID_VOLUME", Severity.BLOCKING, "2018-03-08", imposta("2018-03-08", volume=-5.0)),
    ("HIGH_BELOW_LOW", Severity.BLOCKING, "2018-03-08", imposta("2018-03-08", high=102.0, low=104.0)),
    ("OUT_OF_RANGE", Severity.WARNING, "2018-03-08", imposta("2018-03-08", close=107.0)),
    ("OUT_OF_RANGE", Severity.WARNING, "2018-03-08", imposta("2018-03-08", open=100.0)),
    ("ZERO_VOLUME", Severity.WARNING, "2018-03-08", imposta("2018-03-08", volume=0.0)),
    ("PRICE_JUMP", Severity.BLOCKING, "2018-03-08", salto("2018-03-08", 0.26)),
    ("PRICE_JUMP", Severity.BLOCKING, "2018-03-08", salto("2018-03-08", -0.26)),
    ("ADJ_MISMATCH", Severity.WARNING, "2018-03-15", cedola_ignorata_dal_vendor),
    ("ADJ_MISMATCH", Severity.BLOCKING, "2018-03-08", scala_adj("2018-03-08", 1.10)),
]


@pytest.mark.parametrize(("codice", "severita", "giorno", "guasto"), GUASTI, ids=[g[0] for g in GUASTI])
def test_ogni_controllo_scatta_sul_suo_guasto(
    codice: str, severita: Severity, giorno: str, guasto: Callable[[pd.DataFrame], pd.DataFrame]
) -> None:
    report = check(guasto(serie_pulita()), "SPY", SEDUTE)
    assert (severita.value, codice, pd.Timestamp(giorno).date()) in codici(report.findings)
    assert report.blocking is (severita is Severity.BLOCKING)


QUASI_GUASTI = [
    ("salto del 24%", salto("2018-03-08", 0.24)),
    ("prezzo dimezzato dallo split", lambda f: f),
    ("close sul massimo", imposta("2018-03-08", close=103.5)),
    ("close appena fuori entro tolleranza", imposta("2018-03-08", close=103.5 * 1.0004)),
    ("barra piatta", imposta("2018-03-08", open=103.0, high=103.0, low=103.0, close=103.0)),
    ("volume minimo", imposta("2018-03-08", volume=1.0)),
    ("adj_close con arrotondamento", scala_adj("2018-03-08", 1.00005)),
    ("buco fuori calendario", togli("2018-03-01")),
]


@pytest.mark.parametrize(("nome", "variante"), QUASI_GUASTI, ids=[q[0] for q in QUASI_GUASTI])
def test_nessun_controllo_scatta_sui_casi_quasi_uguali(
    nome: str, variante: Callable[[pd.DataFrame], pd.DataFrame]
) -> None:
    frame = variante(serie_pulita())
    frame["adj_close"] = frame["adj_close"] if nome.startswith("adj") else adj_coerente(frame)
    calendario = SEDUTE[1:] if nome.startswith("buco") else SEDUTE
    assert check(frame, "SPY", calendario).findings == ()


def test_barra_in_giorno_di_chiusura() -> None:
    report = check(serie_pulita(), "SPY", SEDUTE.drop(pd.Timestamp("2018-03-09")))
    assert codici(report.findings) == [("WARNING", "EXTRA_DAY", date(2018, 3, 9))]


def test_serie_ferma_prima_della_fine_del_calendario() -> None:
    corta = serie_pulita().iloc[:-2]
    mancanti = [f.date for f in check(corta, "SPY", SEDUTE).findings if f.code == "MISSING_DAY"]
    assert mancanti == [d.date() for d in SEDUTE[-2:]]


def test_volume_zero_conta_solo_nelle_sedute() -> None:
    frame = imposta("2018-03-09", volume=0.0)(serie_pulita())
    calendario = SEDUTE.drop(pd.Timestamp("2018-03-09"))
    assert [f.code for f in check(frame, "SPY", calendario).findings] == ["EXTRA_DAY"]
    assert [f.code for f in check(frame, "SPY", None).findings] == ["ZERO_VOLUME"]


def test_since_tiene_solo_i_finding_delle_barre_nuove() -> None:
    frame = imposta("2018-03-05", volume=0.0)(imposta("2018-04-05", volume=0.0)(serie_pulita()))
    report = check(frame, "SPY", SEDUTE, since=pd.Timestamp("2018-04-01"))
    assert codici(report.findings) == [("WARNING", "ZERO_VOLUME", date(2018, 4, 5))]


def test_le_soglie_vengono_dalla_configurazione() -> None:
    frame = salto("2018-03-08", 0.15)(serie_pulita())
    assert check(frame, "SPY", SEDUTE).findings == ()
    stretto = check(frame, "SPY", SEDUTE, config=DataQualityConfig(max_close_jump=0.10))
    assert {f.code for f in stretto.findings} == {"PRICE_JUMP"}


def test_senza_adj_close_il_confronto_col_vendor_non_gira() -> None:
    frame = cedola_ignorata_dal_vendor(serie_pulita()).drop(columns="adj_close")
    assert check(frame, "SPY", SEDUTE).findings == ()


def test_calendario_dal_provider() -> None:
    frames = {"SPY": serie_pulita()}
    sedute, origine = build_calendar(frames, lambda inizio, fine: {d.date() for d in SEDUTE})
    assert origine == "alpaca"
    assert sedute.equals(pd.DatetimeIndex(SEDUTE, name="date"))


def test_calendario_ripiega_sull_unione_se_il_provider_fallisce() -> None:
    def rotto(inizio: date, fine: date) -> set[date]:
        raise ConnectionError("rete giu'")

    frames = {"SPY": serie_pulita().iloc[:10], "QQQ": serie_pulita().iloc[5:20]}
    sedute, origine = build_calendar(frames, rotto)
    assert origine.startswith("unione dei simboli") and "rete giu'" in origine
    assert list(sedute) == list(SEDUTE[:20])
    assert build_calendar(frames, None)[1] == "unione dei simboli"


def test_calendario_alpaca_usa_il_client_del_live() -> None:
    richieste: list[object] = []

    def get_calendar(richiesta: object) -> list[SimpleNamespace]:
        richieste.append(richiesta)
        return [SimpleNamespace(date=d.date()) for d in SEDUTE]

    senza_chiavi = load_settings(alpaca_api_key="", alpaca_secret_key="")
    assert alpaca_calendar(senza_chiavi) is None
    provider = alpaca_calendar(senza_chiavi, client=SimpleNamespace(get_calendar=get_calendar))
    assert provider is not None and richieste == []
    assert len(provider(date(2018, 3, 1), date(2018, 4, 11))) == len(SEDUTE)
    assert len(richieste) == 1
