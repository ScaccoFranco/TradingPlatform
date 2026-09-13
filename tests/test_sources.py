"""Contratto di `DataSource` e sorgenti concrete, senza rete: yfinance e HTTP sostituiti da finti."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd
import pytest
import requests

from quant.config import ConfigError, load_settings
from quant.sources import (
    DataSourceError,
    SchemaError,
    TiingoSource,
    YFinanceSource,
    empty_frame,
    validate_schema,
)


def contratto(n: int = 3) -> pd.DataFrame:
    """Frame minimo che rispetta il contratto."""
    indice = pd.bdate_range("2018-03-01", periods=n, name="date")
    return pd.DataFrame(
        {
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.5,
            "volume": 1_000.0,
            "dividend": 0.0,
            "split": 1.0,
            "adj_close": 10.0,
        },
        index=indice,
    )


def test_contratto_valido_passa() -> None:
    validate_schema(contratto())
    validate_schema(contratto().drop(columns="adj_close"))
    validate_schema(empty_frame())


def _senza_nome(f: pd.DataFrame) -> pd.DataFrame:
    return f.rename_axis(None)


def _con_orario(f: pd.DataFrame) -> pd.DataFrame:
    return f.set_axis((f.index + pd.Timedelta(hours=16)).rename("date"))


def _cedola_mancante(f: pd.DataFrame) -> pd.DataFrame:
    f.iloc[1, f.columns.get_loc("dividend")] = float("nan")
    return f


def _split_nullo(f: pd.DataFrame) -> pd.DataFrame:
    f.iloc[1, f.columns.get_loc("split")] = 0.0
    return f


def _cedola_negativa(f: pd.DataFrame) -> pd.DataFrame:
    f.iloc[1, f.columns.get_loc("dividend")] = -0.1
    return f


@pytest.mark.parametrize(
    ("guasto", "messaggio"),
    [
        (lambda f: f.drop(columns="split"), "mancanti"),
        (lambda f: f.assign(vwap=1.0), "non previste"),
        (lambda f: f.astype({"volume": "int64"}), "float64"),
        (lambda f: f.reset_index(drop=True), "DatetimeIndex"),
        (lambda f: f.tz_localize("America/New_York"), "fuso"),
        (_senza_nome, "nome"),
        (_con_orario, "mezzanotte"),
        (_cedola_mancante, "dividend con valori mancanti"),
        (_split_nullo, "non positivi"),
        (_cedola_negativa, "negativi"),
    ],
)
def test_contratto_violato_solleva(guasto: Callable[[pd.DataFrame], pd.DataFrame], messaggio: str) -> None:
    with pytest.raises(SchemaError, match=messaggio):
        validate_schema(guasto(contratto()))


def test_yfinance_smonta_gli_split_e_rispetta_il_contratto(monkeypatch: pytest.MonkeyPatch) -> None:
    """yfinance consegna la scala gia' rettificata: prima dello split 2:1 prezzi e cedole raddoppiano."""
    indice = pd.DatetimeIndex(["2018-03-01", "2018-03-02", "2018-03-05", "2018-03-06"])
    rettificato = pd.DataFrame(
        {
            "Open": [50.0, 51.0, 52.0, 53.0],
            "High": [51.0, 52.0, 53.0, 54.0],
            "Low": [49.0, 50.0, 51.0, 52.0],
            "Close": [50.5, 51.5, 52.5, 53.5],
            "Adj Close": [49.0, 50.0, 51.0, 52.5],
            "Volume": [2_000, 2_000, 1_000, 1_000],
            "Dividends": [0.0, 0.25, 0.0, 0.0],
            "Stock Splits": [0.0, 0.0, 2.0, 0.0],
        },
        index=indice,
    )
    chiamate: list[dict[str, Any]] = []

    def finto_download(symbol: str, **opzioni: Any) -> pd.DataFrame:
        chiamate.append({"symbol": symbol, **opzioni})
        return rettificato

    monkeypatch.setattr("quant.sources.yf.download", finto_download)
    frame = YFinanceSource().fetch("SPY", "2018-03-01", "2018-03-06")

    validate_schema(frame)
    assert chiamate[0]["end"] == "2018-03-07"  # yfinance esclude la data finale
    assert list(frame["close"]) == [101.0, 103.0, 52.5, 53.5]
    assert list(frame["volume"]) == [1_000.0, 1_000.0, 1_000.0, 1_000.0]
    assert list(frame["dividend"]) == [0.0, 0.5, 0.0, 0.0]
    assert list(frame["split"]) == [1.0, 1.0, 2.0, 1.0]
    assert list(frame["adj_close"]) == [49.0, 50.0, 51.0, 52.5]


def test_yfinance_senza_barre_restituisce_frame_vuoto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("quant.sources.yf.download", lambda *a, **k: pd.DataFrame())
    frame = YFinanceSource().fetch("SPY", "2018-03-01")
    assert frame.empty
    validate_schema(frame)


class Risposta:
    def __init__(self, status_code: int, corpo: Any = None, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._corpo = corpo
        self.headers = headers or {}
        self.text = str(corpo)

    def json(self) -> Any:
        return self._corpo


class SessioneFinta:
    """Risponde con una coda di risposte o eccezioni preparate e annota ogni richiesta."""

    def __init__(self, *risposte: Risposta | Exception) -> None:
        self.risposte = list(risposte)
        self.richieste: list[dict[str, Any]] = []

    def get(self, url: str, **opzioni: Any) -> Risposta:
        self.richieste.append({"url": url, **opzioni})
        risposta = self.risposte.pop(0)
        if isinstance(risposta, Exception):
            raise risposta
        return risposta


def record_tiingo(giorno: str, close: float, div: float = 0.0, split: float = 1.0) -> dict[str, Any]:
    return {
        "date": f"{giorno}T00:00:00.000Z",
        "open": close - 1.0,
        "high": close + 1.0,
        "low": close - 2.0,
        "close": close,
        "volume": 1_500_000,
        "adjOpen": close * 0.9 - 1.0,
        "adjHigh": close * 0.9 + 1.0,
        "adjLow": close * 0.9 - 2.0,
        "adjClose": close * 0.9,
        "adjVolume": 1_500_000,
        "divCash": div,
        "splitFactor": split,
    }


def tiingo(sessione: SessioneFinta, **opzioni: Any) -> tuple[TiingoSource, list[float]]:
    attese: list[float] = []
    return TiingoSource("chiave-segreta", session=sessione, sleep=attese.append, **opzioni), attese


def test_tiingo_costruttore_senza_rete_e_chiave_obbligatoria() -> None:
    sessione = SessioneFinta()
    tiingo(sessione)
    assert sessione.richieste == []
    with pytest.raises(ConfigError, match="TIINGO_API_KEY"):
        TiingoSource("")
    sorgente = TiingoSource.from_settings(load_settings(tiingo_api_key="da-settings"), session=sessione)
    assert sorgente._api_key == "da-settings"


def test_tiingo_normalizza_nel_contratto() -> None:
    corpo = [
        record_tiingo("2018-03-02", 102.0, div=0.35),
        record_tiingo("2018-03-01", 101.0),
        record_tiingo("2018-03-05", 51.0, split=2.0),
    ]
    sessione = SessioneFinta(Risposta(200, corpo))
    sorgente, _ = tiingo(sessione)
    frame = sorgente.fetch("SPY", "2018-03-01", "2018-03-05")

    validate_schema(frame)
    richiesta = sessione.richieste[0]
    assert richiesta["url"].endswith("/tiingo/daily/spy/prices")
    assert richiesta["params"] == {"startDate": "2018-03-01", "endDate": "2018-03-05"}
    assert richiesta["headers"]["Authorization"] == "Token chiave-segreta"
    assert list(frame.index.strftime("%Y-%m-%d")) == ["2018-03-01", "2018-03-02", "2018-03-05"]
    assert list(frame["close"]) == [101.0, 102.0, 51.0]
    assert list(frame["dividend"]) == [0.0, 0.35, 0.0]
    assert list(frame["split"]) == [1.0, 1.0, 2.0]
    assert frame.loc["2018-03-01", "adj_close"] == pytest.approx(90.9)
    assert frame.loc["2018-03-01", "volume"] == 1_500_000.0


def test_tiingo_senza_barre_restituisce_frame_vuoto() -> None:
    sorgente, _ = tiingo(SessioneFinta(Risposta(200, [])))
    frame = sorgente.fetch("SPY", "2018-03-01")
    assert frame.empty
    validate_schema(frame)


def test_tiingo_ritenta_dopo_limite_e_errori_di_rete() -> None:
    sessione = SessioneFinta(
        Risposta(429, "troppe richieste", {"Retry-After": "7"}),
        requests.ConnectionError("rete giu'"),
        Risposta(503, "manutenzione"),
        Risposta(200, [record_tiingo("2018-03-01", 101.0)]),
    )
    sorgente, attese = tiingo(sessione, backoff_seconds=1.0)
    assert len(sorgente.fetch("SPY", "2018-03-01")) == 1
    assert attese == [7.0, 2.0, 4.0]


def test_tiingo_rinuncia_dopo_i_tentativi_previsti() -> None:
    sessione = SessioneFinta(*[Risposta(429, "troppe richieste") for _ in range(3)])
    sorgente, attese = tiingo(sessione, max_retries=2, backoff_seconds=1.0)
    with pytest.raises(DataSourceError, match="429 dopo 3 tentativi"):
        sorgente.fetch("SPY", "2018-03-01")
    assert attese == [1.0, 2.0]


def test_tiingo_non_aspetta_oltre_il_massimo() -> None:
    sessione = SessioneFinta(Risposta(429, "limite orario", {"Retry-After": "3600"}))
    sorgente, attese = tiingo(sessione)
    with pytest.raises(DataSourceError, match="riprovare fra 3600s"):
        sorgente.fetch("SPY", "2018-03-01")
    assert attese == []


@pytest.mark.parametrize(
    ("risposta", "messaggio"),
    [
        (Risposta(404, {"detail": "Error: Ticker 'XYZ' not found"}), "sconosciuto"),
        (Risposta(401, {"detail": "Invalid token"}), "chiave Tiingo rifiutata"),
        (Risposta(400, "richiesta malformata"), "400"),
        (Risposta(200, {"detail": "Error: free tier limit"}), "inattesa"),
        (Risposta(200, [{"date": "2018-03-01T00:00:00.000Z", "close": 1.0}]), "campi mancanti"),
    ],
)
def test_tiingo_errori_senza_ritentare(risposta: Risposta, messaggio: str) -> None:
    sorgente, attese = tiingo(SessioneFinta(risposta))
    with pytest.raises(DataSourceError, match=messaggio):
        sorgente.fetch("XYZ", "2018-03-01")
    assert attese == []


def test_la_chiave_non_finisce_nei_messaggi_di_errore() -> None:
    sorgente, _ = tiingo(SessioneFinta(Risposta(401, {"detail": "Invalid token"})))
    with pytest.raises(DataSourceError) as errore:
        sorgente.fetch("SPY", "2018-03-01")
    assert "chiave-segreta" not in str(errore.value)
