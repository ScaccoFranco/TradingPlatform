"""Sorgenti di barre daily indipendenti dal vendor: prezzi grezzi, cedole e split in un solo contratto."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any, ClassVar

import pandas as pd
import requests
import yfinance as yf

from quant.config import ConfigError, Settings, load_settings

PRICE_COLUMNS = ("open", "high", "low", "close", "volume")
REQUIRED_COLUMNS = (*PRICE_COLUMNS, "dividend", "split")
OPTIONAL_COLUMNS = ("adj_close",)

Giorno = date | str


class DataSourceError(RuntimeError):
    """La sorgente non ha consegnato i dati: rete, chiave, limite di richieste, simbolo sconosciuto."""


class SchemaError(ValueError):
    """Il DataFrame non rispetta il contratto di `DataSource.fetch`."""


class DataSource(ABC):
    """Fonte di barre daily.

    Contratto del DataFrame restituito da `fetch`:

    - indice `DatetimeIndex` di nome `date`, senza fuso, una seduta per riga a mezzanotte,
      in ordine crescente e senza ripetizioni;
    - `open, high, low, close, volume`: valori grezzi, cioe' come scambiati quel giorno,
      senza alcuna rettifica per split o cedole;
    - `dividend`: cassa per azione con data ex quel giorno, sulla scala grezza di quel giorno,
      0.0 se non c'e' cedola;
    - `split`: fattore dello split con data ex quel giorno, 2.0 per un 2:1, 1.0 se assente;
    - `adj_close`, facoltativa: la serie rettificata del vendor, buona solo per confronto;
    - tutte le colonne in float64, nessun'altra colonna.

    `start` ed `end` sono inclusi, `end=None` arriva all'ultima barra disponibile. Un periodo
    senza barre produce un DataFrame vuoto con le stesse colonne; un errore della sorgente
    solleva `DataSourceError`. `validate_schema` verifica la forma; ordine, unicita' e
    plausibilita' dei valori spettano ai controlli di qualita'.
    """

    name: ClassVar[str]

    @abstractmethod
    def fetch(self, symbol: str, start: Giorno, end: Giorno | None = None) -> pd.DataFrame:
        """Barre del simbolo fra start ed end inclusi, nel formato del contratto."""


def empty_frame() -> pd.DataFrame:
    """DataFrame senza barre ma con le colonne e l'indice del contratto."""
    return pd.DataFrame(
        {colonna: pd.Series(dtype="float64") for colonna in REQUIRED_COLUMNS},
        index=pd.DatetimeIndex([], name="date"),
    )


def validate_schema(frame: pd.DataFrame) -> None:
    """Solleva `SchemaError` con l'elenco dei problemi se il frame viola il contratto."""
    problemi: list[str] = []
    indice = frame.index
    if not isinstance(indice, pd.DatetimeIndex):
        problemi.append(f"indice {type(indice).__name__} invece di DatetimeIndex")
    else:
        if indice.name != "date":
            problemi.append(f"indice di nome {indice.name!r} invece di 'date'")
        if indice.tz is not None:
            problemi.append(f"indice con fuso {indice.tz}")
        elif len(indice) and not (indice == indice.normalize()).all():
            problemi.append("date con orario diverso dalla mezzanotte")

    ammesse = (*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS)
    mancanti = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    estranee = [c for c in frame.columns if c not in ammesse]
    if mancanti:
        problemi.append(f"colonne mancanti {mancanti}")
    if estranee:
        problemi.append(f"colonne non previste {estranee}")
    non_float = [c for c in frame.columns if c in ammesse and frame[c].dtype != "float64"]
    if non_float:
        problemi.append(f"colonne non float64 {non_float}")

    for colonna in ("dividend", "split"):
        if colonna in frame.columns and frame[colonna].isna().any():
            problemi.append(f"{colonna} con valori mancanti")
    if "split" in frame.columns and (frame["split"] <= 0.0).any():
        problemi.append("split con fattori non positivi")
    if "dividend" in frame.columns and (frame["dividend"] < 0.0).any():
        problemi.append("dividend negativi")

    if problemi:
        raise SchemaError("; ".join(problemi))


def _giorno(valore: Giorno) -> date:
    giorno: date = pd.Timestamp(valore).date()
    return giorno


class YFinanceSource(DataSource):
    """yfinance, la fonte storica del progetto: resta per retrocompatibilita' e come confronto.

    Non garantisce prezzi stabili nel tempo e consegna la scala gia' rettificata per gli
    split, che qui viene smontata per tornare ai prezzi grezzi.
    """

    name = "yfinance"

    def fetch(self, symbol: str, start: Giorno, end: Giorno | None = None) -> pd.DataFrame:
        """Scarica barre, cedole e split e le riporta ai prezzi grezzi."""
        fine = None if end is None else (_giorno(end) + timedelta(days=1)).isoformat()  # yfinance esclude end
        raw = yf.download(
            symbol,
            start=_giorno(start).isoformat(),
            end=fine,
            interval="1d",
            auto_adjust=False,
            actions=True,
            progress=False,
            multi_level_index=False,
        )
        if raw is None or raw.empty:
            return empty_frame()
        frame = raw.rename(columns={c: str(c).lower().replace(" ", "_") for c in raw.columns})
        attese = ["open", "high", "low", "close", "adj_close", "volume"]
        missing = [c for c in attese if c not in frame.columns]
        if missing:
            raise DataSourceError(f"{symbol}: colonne mancanti {missing}")
        frame.index = pd.DatetimeIndex(frame.index).tz_localize(None)
        frame.index.name = "date"
        return smonta_split(frame.sort_index())


def smonta_split(frame: pd.DataFrame) -> pd.DataFrame:
    """Riporta i prezzi yfinance alla scala del giorno in cui furono scambiati davvero.

    yfinance restituisce prezzi e cedole gia' rettificati per gli split, quindi una serie
    continua. Il broker invece consegna prezzi grezzi e cambia le azioni il giorno dello
    split: moltiplicando ogni barra per il prodotto degli split successivi si torna al suo
    mondo. `adj_close` resta la serie rettificata del vendor.
    """
    grezzi = frame.get("stock_splits")
    fattori = pd.Series(1.0, index=frame.index) if grezzi is None else grezzi.fillna(0.0).replace(0.0, 1.0)
    cedole = frame.get("dividends", pd.Series(0.0, index=frame.index)).fillna(0.0)

    # moltiplicatore da applicare a una barra: prodotto degli split successivi a quella data
    successivi = fattori[::-1].shift(1, fill_value=1.0).cumprod()[::-1]
    grezzo = pd.DataFrame(
        {
            "open": frame["open"] * successivi,
            "high": frame["high"] * successivi,
            "low": frame["low"] * successivi,
            "close": frame["close"] * successivi,
            "volume": frame["volume"] / successivi,
            "dividend": cedole * successivi,
            "split": fattori,
            "adj_close": frame["adj_close"],
        },
        index=frame.index,
    )
    return grezzo.dropna().astype("float64")


TIINGO_URL = "https://api.tiingo.com/tiingo/daily/{symbol}/prices"
TIINGO_COLUMNS = {
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "divCash": "dividend",
    "splitFactor": "split",
    "adjClose": "adj_close",
}


class TiingoSource(DataSource):
    """Daily EOD di Tiingo: prezzi grezzi con `divCash` e `splitFactor` gia' espliciti.

    L'endpoint restituisce l'intero intervallo in una sola risposta, quindi non serve
    paginare. Il limite di richieste del piano gratuito (poche decine l'ora) basta per
    l'universo attuale: un 429 o un errore del server si ritenta con attesa crescente,
    rispettando `Retry-After`, e oltre `max_wait` si rinuncia con un errore esplicito.
    Il costruttore non tocca la rete.
    """

    name = "tiingo"

    def __init__(
        self,
        api_key: str,
        *,
        session: Any = None,
        timeout: float = 30.0,
        max_retries: int = 4,
        backoff_seconds: float = 2.0,
        max_wait: float = 120.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ConfigError("TIINGO_API_KEY mancante: impostarla nell'ambiente o nel file .env")
        self._api_key = api_key
        self._http = session if session is not None else requests
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.max_wait = max_wait
        self._sleep = sleep

    @classmethod
    def from_settings(cls, settings: Settings | None = None, **opzioni: Any) -> TiingoSource:
        """Sorgente con la chiave letta da `Settings`, cioe' da ambiente o `.env`."""
        impostazioni = settings or load_settings()
        return cls(impostazioni.tiingo_api_key, **opzioni)

    def fetch(self, symbol: str, start: Giorno, end: Giorno | None = None) -> pd.DataFrame:
        """Barre daily grezze del simbolo, con cedole, split e `adj_close` del vendor."""
        parametri = {"startDate": _giorno(start).isoformat()}
        if end is not None:
            parametri["endDate"] = _giorno(end).isoformat()
        return _normalizza_tiingo(symbol, self._richiedi(symbol, parametri))

    def _richiedi(self, symbol: str, parametri: dict[str, str]) -> Any:
        url = TIINGO_URL.format(symbol=symbol.lower())
        intestazioni = {"Authorization": f"Token {self._api_key}", "Content-Type": "application/json"}
        for tentativo in range(self.max_retries + 1):
            ultimo = tentativo == self.max_retries
            try:
                risposta = self._http.get(url, params=parametri, headers=intestazioni, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as errore:
                if ultimo:
                    raise DataSourceError(f"{symbol}: Tiingo non raggiungibile ({errore})") from errore
                self._attendi(self.backoff_seconds * 2.0**tentativo, symbol)
                continue
            except requests.RequestException as errore:
                raise DataSourceError(f"{symbol}: richiesta a Tiingo fallita ({errore})") from errore

            stato = risposta.status_code
            if stato == 429 or stato >= 500:
                if ultimo:
                    raise DataSourceError(f"{symbol}: Tiingo risponde {stato} dopo {tentativo + 1} tentativi")
                self._attendi(self._attesa(risposta, tentativo), symbol)
                continue
            if stato in (401, 403):
                raise DataSourceError(f"{symbol}: chiave Tiingo rifiutata ({stato})")
            if stato == 404:
                raise DataSourceError(f"{symbol}: simbolo sconosciuto a Tiingo")
            if stato != 200:
                raise DataSourceError(f"{symbol}: Tiingo risponde {stato}: {risposta.text[:200]}")
            try:
                return risposta.json()
            except ValueError as errore:
                raise DataSourceError(f"{symbol}: risposta Tiingo non in JSON") from errore
        raise AssertionError("irraggiungibile")  # pragma: no cover

    def _attesa(self, risposta: Any, tentativo: int) -> float:
        try:
            return float(risposta.headers.get("Retry-After", ""))
        except ValueError:
            return self.backoff_seconds * 2.0**tentativo

    def _attendi(self, secondi: float, symbol: str) -> None:
        if secondi > self.max_wait:
            raise DataSourceError(f"{symbol}: limite di richieste Tiingo, riprovare fra {secondi:.0f}s")
        self._sleep(secondi)


def _normalizza_tiingo(symbol: str, corpo: Any) -> pd.DataFrame:
    """Dalla lista di record JSON di Tiingo al DataFrame del contratto."""
    if not isinstance(corpo, list):
        raise DataSourceError(f"{symbol}: risposta Tiingo inattesa: {str(corpo)[:200]}")
    if not corpo:
        return empty_frame()
    frame = pd.DataFrame.from_records(corpo)
    mancanti = [c for c in ("date", *TIINGO_COLUMNS) if c not in frame.columns]
    if mancanti:
        raise DataSourceError(f"{symbol}: campi mancanti nella risposta Tiingo {mancanti}")
    date_utc = pd.to_datetime(frame["date"], utc=True)
    indice = pd.DatetimeIndex(date_utc).tz_convert(None).normalize()
    normalizzato = frame[list(TIINGO_COLUMNS)].rename(columns=TIINGO_COLUMNS).astype("float64")
    normalizzato.index = indice.rename("date")
    return normalizzato.sort_index()
