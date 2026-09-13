"""Controlli di qualita' sulle barre in ingresso, prima che diventino Parquet.

Ogni controllo produce `Finding` con una severita': `BLOCKING` impedisce la scrittura,
`WARNING` scrive e segnala. Il frame controllato e' quello del contratto di
`quant.sources.DataSource`, con `dividend`, `split` e l'`adj_close` del vendor.

| Codice | Severita' | Cosa rileva |
|---|---|---|
| DUPLICATE_DATE | BLOCKING | stessa data su piu' barre |
| UNSORTED_INDEX | BLOCKING | data precedente a quella della barra prima |
| MISSING_DAY | WARNING | seduta del calendario senza barra |
| EXTRA_DAY | WARNING | barra in un giorno di chiusura |
| INVALID_PRICE | BLOCKING | prezzo mancante, nullo o negativo, adj_close del vendor compreso |
| INVALID_VOLUME | BLOCKING | volume mancante o negativo |
| HIGH_BELOW_LOW | BLOCKING | high minore di low |
| OUT_OF_RANGE | WARNING | open o close fuori da [low, high] oltre la tolleranza |
| ZERO_VOLUME | WARNING | volume zero in una seduta |
| PRICE_JUMP | BLOCKING | close che salta oltre soglia senza uno split che lo spieghi |
| ADJ_MISMATCH | WARNING o BLOCKING | `adj_close` del vendor incoerente con cedole e split |
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

import pandas as pd

from quant.adjust import step_factors
from quant.config import DataQualityConfig, Settings, load_settings

PREZZI = ("open", "high", "low", "close")

CalendarProvider = Callable[[date, date], Collection[date]]


class Severity(StrEnum):
    """Gravita' di un finding."""

    BLOCKING = "BLOCKING"
    WARNING = "WARNING"


@dataclass(frozen=True, slots=True)
class Finding:
    """Un problema trovato nei dati, con la data a cui si riferisce se ne ha una."""

    severity: Severity
    code: str
    date: date | None
    message: str


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    """Esito dei controlli su un simbolo."""

    symbol: str
    findings: tuple[Finding, ...]

    @property
    def blocking(self) -> bool:
        """True se almeno un finding impedisce la scrittura."""
        return any(f.severity is Severity.BLOCKING for f in self.findings)

    def count(self, severity: Severity) -> int:
        """Quanti finding hanno la severita' data."""
        return sum(1 for f in self.findings if f.severity is severity)


def check(
    df: pd.DataFrame,
    symbol: str,
    calendar: pd.DatetimeIndex | None = None,
    *,
    since: pd.Timestamp | date | None = None,
    config: DataQualityConfig | None = None,
) -> DataQualityReport:
    """Esegue tutti i controlli sul frame di un simbolo.

    `calendar` sono le sedute di borsa attese: senza, i controlli sui giorni mancanti o in
    piu' non girano. `since` limita i finding alle date da quella in poi: con un
    aggiornamento incrementale le barre vecchie servono da contesto, per esempio per il
    salto fra l'ultima barra salvata e la prima nuova, ma sono gia' state riportate.
    """
    soglie = config or DataQualityConfig()
    indice = pd.DatetimeIndex(df.index)
    findings = [*_duplicati(indice), *_ordine(indice)]
    frame = df[~indice.duplicated(keep="last")].sort_index()

    findings += _prezzi_invalidi(frame)
    findings += _volumi_invalidi(frame)
    findings += _range(frame, soglie.range_tolerance)
    findings += _salti(frame, soglie.max_close_jump)
    findings += _rettifica(frame, soglie)
    sedute = None if calendar is None else pd.DatetimeIndex(calendar).normalize()
    findings += _volume_zero(frame, sedute)
    if sedute is not None:
        findings += _calendario(frame, sedute)

    if since is not None:
        limite = pd.Timestamp(since).date()
        findings = [f for f in findings if f.date is None or f.date >= limite]
    findings.sort(key=lambda f: (f.date or date.min, f.code))
    return DataQualityReport(symbol, tuple(findings))


def _giorno(valore: Any) -> date:
    giorno: date = pd.Timestamp(valore).date()
    return giorno


def _duplicati(indice: pd.DatetimeIndex) -> list[Finding]:
    ripetute = indice[indice.duplicated(keep=False)]
    return [
        Finding(Severity.BLOCKING, "DUPLICATE_DATE", _giorno(g), f"data presente {int(n)} volte")
        for g, n in ripetute.value_counts().sort_index().items()
    ]


def _ordine(indice: pd.DatetimeIndex) -> list[Finding]:
    if len(indice) < 2:
        return []
    precedenti, correnti = indice[:-1], indice[1:]
    fuori_posto = correnti < precedenti
    return [
        Finding(Severity.BLOCKING, "UNSORTED_INDEX", _giorno(c), f"segue la barra del {_giorno(p)}")
        for p, c in zip(precedenti[fuori_posto], correnti[fuori_posto], strict=True)
    ]


def _prezzi_invalidi(frame: pd.DataFrame) -> list[Finding]:
    colonne = [c for c in (*PREZZI, "adj_close") if c in frame.columns]
    guasti = frame[colonne].isna() | (frame[colonne] <= 0.0)
    righe = guasti[guasti.any(axis=1)]
    return [
        Finding(
            Severity.BLOCKING,
            "INVALID_PRICE",
            _giorno(g),
            "mancante, nullo o negativo: " + ", ".join(f"{c}={frame.at[g, c]}" for c in colonne if riga[c]),
        )
        for g, riga in righe.iterrows()
    ]


def _volumi_invalidi(frame: pd.DataFrame) -> list[Finding]:
    volume = frame["volume"]
    guasti = volume[volume.isna() | (volume < 0.0)]
    return [
        Finding(Severity.BLOCKING, "INVALID_VOLUME", _giorno(g), f"volume={v}") for g, v in guasti.items()
    ]


def _range(frame: pd.DataFrame, tolleranza: float) -> list[Finding]:
    findings: list[Finding] = []
    invertite = frame["high"] < frame["low"]
    for g in frame.index[invertite]:
        messaggio = f"high {frame.at[g, 'high']:g} sotto low {frame.at[g, 'low']:g}"
        findings.append(Finding(Severity.BLOCKING, "HIGH_BELOW_LOW", _giorno(g), messaggio))
    sopra = frame["high"] * (1.0 + tolleranza)
    sotto = frame["low"] * (1.0 - tolleranza)
    for colonna in ("open", "close"):
        fuori = ((frame[colonna] > sopra) | (frame[colonna] < sotto)) & ~invertite
        for g in frame.index[fuori]:
            valore, minimo, massimo = frame.at[g, colonna], frame.at[g, "low"], frame.at[g, "high"]
            messaggio = f"{colonna} {valore:g} fuori da [{minimo:g}, {massimo:g}]"
            findings.append(Finding(Severity.WARNING, "OUT_OF_RANGE", _giorno(g), messaggio))
    return findings


def _salti(frame: pd.DataFrame, soglia: float) -> list[Finding]:
    """Variazione del close al netto dello split della barra: uno split 2:1 dimezza il prezzo."""
    precedente = frame["close"].shift(1)
    variazione = frame["close"] * frame["split"] / precedente - 1.0
    findings: list[Finding] = []
    for g in frame.index[variazione.abs() > soglia]:
        split = frame.at[g, "split"]
        contesto = "senza split" if split == 1.0 else f"anche tenendo conto dello split {split:g}"
        messaggio = f"close da {precedente[g]:g} a {frame.at[g, 'close']:g} ({variazione[g]:+.1%}) {contesto}"
        findings.append(Finding(Severity.BLOCKING, "PRICE_JUMP", _giorno(g), messaggio))
    return findings


def rendimenti_ricostruiti(frame: pd.DataFrame) -> pd.Series:
    """Rendimento giornaliero rettificato che si ricava dai grezzi con il metodo di `quant.adjust`.

    Si confronta barra per barra invece che sul fattore cumulativo: un solo errore del
    vendor resta sulla sua data invece di spostare la scala di tutta la storia precedente.
    """
    passi = step_factors(frame["close"], frame["dividend"], frame["split"])
    rendimenti: pd.Series = frame["close"] / (frame["close"].shift(1) * passi) - 1.0
    return rendimenti


def _rettifica(frame: pd.DataFrame, soglie: DataQualityConfig) -> list[Finding]:
    """`adj_close` del vendor contro il calcolo in casa; le barre con adj_close NaN non si confrontano."""
    if "adj_close" not in frame.columns:
        return []
    vendor = frame["adj_close"] / frame["adj_close"].shift(1) - 1.0
    ricostruito = rendimenti_ricostruiti(frame)
    scarto = ((1.0 + vendor) / (1.0 + ricostruito) - 1.0).abs()
    findings: list[Finding] = []
    for g in frame.index[scarto > soglie.adj_close_warning]:
        severita = Severity.BLOCKING if scarto[g] > soglie.adj_close_blocking else Severity.WARNING
        messaggio = (
            f"rendimento rettificato del vendor {vendor[g]:+.4%} contro {ricostruito[g]:+.4%} "
            f"da grezzi, cedola {frame.at[g, 'dividend']:g} e split {frame.at[g, 'split']:g}"
        )
        findings.append(Finding(severita, "ADJ_MISMATCH", _giorno(g), messaggio))
    return findings


def _volume_zero(frame: pd.DataFrame, sedute: pd.DatetimeIndex | None) -> list[Finding]:
    zero = frame["volume"] == 0.0
    if sedute is not None:
        zero &= frame.index.isin(sedute)
    return [Finding(Severity.WARNING, "ZERO_VOLUME", _giorno(g), "volume zero") for g in frame.index[zero]]


def _calendario(frame: pd.DataFrame, sedute: pd.DatetimeIndex) -> list[Finding]:
    """Sedute senza barra dalla prima barra alla fine del calendario, e barre fuori calendario.

    Si guarda fino alla fine del calendario, non all'ultima barra del simbolo: una serie che
    si ferma prima delle altre e' proprio uno dei buchi da segnalare.
    """
    if frame.empty or sedute.empty:
        return []
    attese = sedute[(sedute >= frame.index[0]) & (sedute <= sedute.max())]
    mancanti = attese.difference(frame.index)
    coperte = frame.index[(frame.index >= sedute.min()) & (frame.index <= sedute.max())]
    in_piu = coperte.difference(sedute)
    return [
        *(Finding(Severity.WARNING, "MISSING_DAY", _giorno(g), "seduta senza barra") for g in mancanti),
        *(
            Finding(Severity.WARNING, "EXTRA_DAY", _giorno(g), "barra in un giorno di chiusura")
            for g in in_piu
        ),
    ]


def alpaca_calendar(settings: Settings | None = None, client: Any = None) -> CalendarProvider | None:
    """Sedute dal calendario Alpaca, lo stesso del live; None se mancano le chiavi.

    Il client si crea solo alla prima richiesta, e la richiesta puo' fallire: chi usa il
    provider deve prevedere il ripiego, vedi `build_calendar`.
    """
    impostazioni = settings or load_settings()
    if client is None and not (impostazioni.alpaca_api_key and impostazioni.alpaca_secret_key):
        return None

    def provider(start: date, end: date) -> Collection[date]:
        from quant.live.schedule import trading_days

        cliente = client
        if cliente is None:
            from alpaca.trading.client import TradingClient

            cliente = TradingClient(
                api_key=impostazioni.alpaca_api_key,
                secret_key=impostazioni.alpaca_secret_key,
                paper=impostazioni.alpaca_paper,
            )
        return trading_days(cliente, start, end)

    return provider


def build_calendar(
    frames: dict[str, pd.DataFrame], provider: CalendarProvider | None = None
) -> tuple[pd.DatetimeIndex, str]:
    """Sedute attese sul periodo coperto dai frame, con l'origine del calendario.

    Prima il provider, di norma Alpaca. Se manca o non risponde si ripiega sull'unione
    delle date dei simboli scaricati: un giorno assente in tutti non si vede, ma un buco
    in un solo simbolo si'.
    """
    pieni = [f for f in frames.values() if not f.empty]
    if not pieni:
        return pd.DatetimeIndex([], name="date"), "vuoto"
    inizio = min(pd.Timestamp(f.index.min()) for f in pieni)
    fine = max(pd.Timestamp(f.index.max()) for f in pieni)
    if provider is not None:
        try:
            sedute = pd.DatetimeIndex(sorted(provider(inizio.date(), fine.date())), name="date")
            if not sedute.empty:
                return sedute.normalize(), "alpaca"
            motivo = "calendario vuoto"
        except Exception as errore:  # noqa: BLE001 - il calendario e' un servizio esterno
            motivo = str(errore)
        origine = f"unione dei simboli (calendario non disponibile: {motivo})"
    else:
        origine = "unione dei simboli"
    unione = pd.DatetimeIndex([], name="date")
    for frame in pieni:
        unione = unione.union(pd.DatetimeIndex(frame.index).normalize())
    return unione.rename("date"), origine
