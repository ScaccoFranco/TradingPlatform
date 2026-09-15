"""Strato dati della dashboard: StateStore, log, Parquet e file KILL, tutto in sola lettura.

Qui non si calcola niente: posizioni, equity, ordini ed eseguiti arrivano da
`quant.state`, decisioni del rischio, esecuzioni e anomalie da `quant.logreader`, il
calendario di borsa da `quant.weekly`, l'interruttore da `quant.risk`. Una sorgente
assente o illeggibile diventa un risultato con `disponibile=False` e una nota che dice
perche', mai un'eccezione: la dashboard deve restare leggibile anche prima della prima
esecuzione live.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from quant.config import PERCORSO_DATI
from quant.logreader import (
    ORDINE_RIDOTTO,
    ORDINE_RIFIUTATO,
    PERCORSO_LOG,
    RUN_FALLITA,
    anomalies,
    decisions_by_reason,
    event_time,
    last_run,
    read_events,
    risk_decisions,
)
from quant.reconcile_report import (
    SOLO_SHADOW,
    FillComparison,
    FillRecord,
    compare,
    real_fills,
    reference_opens,
    theoretical_fills,
)
from quant.risk import PERCORSO_KILL, KillSwitch
from quant.shadow import ShadowResult
from quant.state import PERCORSO_DB, StateStore, StoredFill, StoredOrder
from quant.weekly import REPORT, ConfrontoLive, confronto_live, giorni_di_borsa_dopo, shadow_live

FUSO_BORSA = ZoneInfo("America/New_York")  # quello di quant.live.schedule, che qui non si importa
GIORNI_EQUITY = 30
GIORNI_DECISIONI = 7
GIORNI_ANOMALIE = 7
RIGHE = 20
GIORNI_ORDINI = 30
RIGHE_ORDINI = 500
SCELTE_GIORNI = (1, 7, 30, 90)
SIMBOLO_VALIDO = re.compile(r"[A-Z0-9.\-]{1,12}")
TIPO_TESTO = "testo"
TIPO_IMMAGINE = "immagine"
TIPO_ALTRO = "altro"
SUFFISSI_TESTO = frozenset({".md", ".csv", ".txt", ".json", ".log"})
TIPI_IMMAGINE = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
MASSIMO_TESTO = 2_000_000
MASSIMO_FILE_REPORT = 2_000
DB_ASSENTE = "database non ancora creato: nessuna esecuzione live registrata"
CAMPI_DI_SERVIZIO = frozenset({"event", "timestamp", "level", "run_id", "symbol", "exception"})

Evento = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Sorgenti:
    """Percorsi letti dalla dashboard, iniettati cosi' i test non guardano quelli veri."""

    db: Path = PERCORSO_DB
    data_dir: Path = PERCORSO_DATI
    reports_dir: Path = REPORT
    log: Path = PERCORSO_LOG
    kill: Path = PERCORSO_KILL


@dataclass(frozen=True, slots=True)
class Posizione:
    """Posizione attesa secondo lo stato salvato."""

    symbol: str
    quantita: int


@dataclass(frozen=True, slots=True)
class StatoKillSwitch:
    """Interruttore di emergenza come lo vede il runner: il file `KILL` e il suo motivo."""

    attivo: bool
    motivo: str
    percorso: Path


@dataclass(frozen=True, slots=True)
class UltimaEsecuzione:
    """Ultima passata del runner registrata nel log."""

    momento: datetime | None
    run_id: str | None
    esito: str

    @property
    def fallita(self) -> bool:
        """True se la passata e' finita con un'eccezione."""
        return self.esito == RUN_FALLITA


@dataclass(frozen=True, slots=True)
class StatoLive:
    """Fotografia dello stato live, quella di `scripts/status.py` piu' interruttore ed esecuzione.

    `disponibile` e `nota` riguardano il database, `nota_log` il file di log: le due
    sorgenti possono mancare l'una indipendentemente dall'altra.
    """

    disponibile: bool
    nota: str
    posizioni: tuple[Posizione, ...]
    giorno_equity: date | None
    cassa: float | None
    valore_posizioni: float | None
    equity: float | None
    kill_switch: StatoKillSwitch
    ultima_esecuzione: UltimaEsecuzione | None
    nota_log: str


@dataclass(frozen=True, slots=True)
class PuntoEquity:
    """Rilevazione giornaliera dell'equity live."""

    giorno: date
    cassa: float
    valore_posizioni: float
    totale: float


@dataclass(frozen=True, slots=True)
class Elenco[T]:
    """Righe lette da una sorgente. `disponibile` falso vuol dire sorgente assente, non vuota."""

    disponibile: bool
    nota: str
    righe: tuple[T, ...] = ()


@dataclass(frozen=True, slots=True)
class Finestra[T]:
    """Righe di log in una finestra di giornate, estremi compresi."""

    disponibile: bool
    nota: str
    dal: date
    al: date
    righe: tuple[T, ...] = ()


@dataclass(frozen=True, slots=True)
class DecisioneRischio:
    """Decisione del gestore del rischio come l'ha scritta nel log."""

    momento: datetime | None
    evento: str
    symbol: str
    quantita_richiesta: int | None
    quantita_finale: int | None
    motivo: str
    run_id: str | None

    @property
    def rifiutata(self) -> bool:
        """True se l'ordine non e' arrivato all'esecuzione."""
        return self.evento == ORDINE_RIFIUTATO

    @property
    def ridotta(self) -> bool:
        """True se l'ordine e' passato con una quantita' minore di quella richiesta."""
        return self.evento == ORDINE_RIDOTTO


@dataclass(frozen=True, slots=True)
class Anomalia:
    """Evento di log che chiede l'attenzione di una persona, con i campi che lo descrivono."""

    momento: datetime | None
    evento: str
    symbol: str | None
    run_id: str | None
    campi: tuple[tuple[str, str], ...]

    def campo(self, nome: str) -> str | None:
        """Valore di un campo del log, None se l'evento non lo porta."""
        return dict(self.campi).get(nome)

    @property
    def dettaglio(self) -> str:
        """Campi dell'evento in una riga, per le tabelle."""
        return ", ".join(f"{chiave}={valore}" for chiave, valore in self.campi)


@dataclass(frozen=True, slots=True)
class Ritardo:
    """Giornate di borsa trascorse dall'ultima esecuzione nota, oggi compreso."""

    ultima: date
    fonte: str
    sedute: tuple[date, ...]
    nota: str = ""


type CalcoloOmbra = Callable[[date, Path], ShadowResult | None]


@dataclass(frozen=True, slots=True, eq=False)
class Performance:
    """Confronto fra live, shadow e mercato, con le note sulle sorgenti che mancano."""

    confronto: ConfrontoLive | None
    nota: str
    nota_shadow: str


@dataclass(frozen=True, slots=True)
class FiltroOrdini:
    """Filtro della pagina ordini: giornate comprese e simbolo, con i valori scartati."""

    dal: date
    al: date
    simbolo: str | None
    errori: tuple[str, ...] = ()

    def comprende(self, giorno: date, simbolo: str) -> bool:
        """True se la giornata e il simbolo cadono nel filtro."""
        return self.dal <= giorno <= self.al and (self.simbolo is None or simbolo == self.simbolo)


@dataclass(frozen=True, slots=True)
class RigaEseguito:
    """Eseguito reale con il suo confronto con lo shadow, se lo shadow ha un fill quel giorno."""

    fill: StoredFill
    confronto: FillComparison | None


@dataclass(frozen=True, slots=True)
class OrdiniEseguiti:
    """Ordini ed eseguiti nel filtro, piu' i fill teorici che nel live non ci sono."""

    disponibile: bool
    nota: str
    ordini: tuple[StoredOrder, ...] = ()
    eseguiti: tuple[RigaEseguito, ...] = ()
    solo_shadow: tuple[FillComparison, ...] = ()
    in_attesa: int = 0
    troncato: bool = False


@dataclass(frozen=True, slots=True)
class GruppoMotivo:
    """Decisioni in cui ha agito un certo motivo di RiskReason."""

    motivo: str
    decisioni: tuple[DecisioneRischio, ...]

    @property
    def ridotte(self) -> tuple[DecisioneRischio, ...]:
        """Ordini passati con una quantita' minore."""
        return tuple(d for d in self.decisioni if d.ridotta)

    @property
    def rifiutate(self) -> tuple[DecisioneRischio, ...]:
        """Ordini fermati."""
        return tuple(d for d in self.decisioni if d.rifiutata)


@dataclass(frozen=True, slots=True)
class Rischio:
    """Decisioni del rischio e anomalie di una finestra, lette dal log in una volta."""

    disponibile: bool
    nota: str
    dal: date
    al: date
    decisioni: tuple[DecisioneRischio, ...] = ()
    gruppi: tuple[GruppoMotivo, ...] = ()
    anomalie: tuple[Anomalia, ...] = ()

    @property
    def ridotte(self) -> tuple[DecisioneRischio, ...]:
        """Ordini passati con una quantita' minore."""
        return tuple(d for d in self.decisioni if d.ridotta)

    @property
    def rifiutate(self) -> tuple[DecisioneRischio, ...]:
        """Ordini fermati."""
        return tuple(d for d in self.decisioni if d.rifiutata)

    @property
    def approvate(self) -> tuple[DecisioneRischio, ...]:
        """Ordini passati intatti: in live sono loggati a DEBUG e di solito non compaiono."""
        return tuple(d for d in self.decisioni if not d.ridotta and not d.rifiutata)


@dataclass(frozen=True, slots=True)
class FileReport:
    """File della cartella dei report, con il percorso relativo a quella cartella."""

    nome: str
    modificato: datetime
    dimensione: int

    @property
    def tipo(self) -> str:
        """Come la dashboard lo mostra: testo, immagine o niente."""
        return tipo_report(self.nome)


@contextmanager
def apri_sola_lettura(percorso: str | Path) -> Iterator[StateStore | None]:
    """StateStore in sola lettura imposta dal driver, None se il database non esiste ancora.

    Nessun file o cartella viene creato; la connessione si chiude all'uscita dal blocco.
    """
    if not Path(percorso).is_file():
        yield None
        return
    store = StateStore.read_only(percorso)
    try:
        yield store
    finally:
        store.close()


def oggi_in_borsa() -> date:
    """Data di oggi a New York: dopo mezzanotte in Europa la seduta americana e' ancora ieri."""
    return datetime.now(FUSO_BORSA).date()


def stato_live(sorgenti: Sorgenti) -> StatoLive:
    """Posizioni attese, cassa ed equity dell'ultima rilevazione, kill switch, ultima esecuzione."""
    letto, nota = _dal_database(sorgenti.db, lambda store: (store.load_positions(), store.load_equity(1)))
    posizioni, equity = letto if letto is not None else ({}, [])
    ultima = equity[-1] if equity else None
    esecuzione, nota_log = _ultima_esecuzione(sorgenti.log)
    return StatoLive(
        disponibile=letto is not None,
        nota=nota,
        posizioni=tuple(Posizione(symbol, quantita) for symbol, quantita in sorted(posizioni.items())),
        giorno_equity=ultima[0] if ultima else None,
        cassa=ultima[1] if ultima else None,
        valore_posizioni=ultima[2] if ultima else None,
        equity=ultima[3] if ultima else None,
        kill_switch=stato_kill_switch(sorgenti.kill),
        ultima_esecuzione=esecuzione,
        nota_log=nota_log,
    )


def stato_kill_switch(percorso: Path) -> StatoKillSwitch:
    """Legge l'interruttore senza toccarlo: la dashboard non lo attiva ne' lo rimuove."""
    interruttore = KillSwitch(percorso)
    try:
        attivo = interruttore.is_active()
        motivo = interruttore.reason() if attivo else ""
    except OSError as errore:
        attivo, motivo = True, f"file {percorso} presente ma non leggibile: {errore}"
    return StatoKillSwitch(attivo, motivo, percorso)


def serie_equity(sorgenti: Sorgenti, giorni: int = GIORNI_EQUITY) -> Elenco[PuntoEquity]:
    """Ultime `giorni` rilevazioni di equity, dalla piu' vecchia alla piu' recente."""
    righe, nota = _dal_database(sorgenti.db, lambda store: store.load_equity(giorni))
    if righe is None:
        return Elenco(False, nota)
    punti = tuple(PuntoEquity(g, cassa, valore, totale) for g, cassa, valore, totale in righe)
    return Elenco(True, "", punti)


def ordini_recenti(sorgenti: Sorgenti, n: int = RIGHE) -> Elenco[StoredOrder]:
    """Ultimi `n` ordini registrati, dal piu' recente."""
    righe, nota = _dal_database(sorgenti.db, lambda store: store.recent_orders(n))
    return Elenco(False, nota) if righe is None else Elenco(True, "", tuple(righe))


def fill_recenti(sorgenti: Sorgenti, n: int = RIGHE) -> Elenco[StoredFill]:
    """Ultimi `n` eseguiti registrati, dal piu' recente."""
    righe, nota = _dal_database(sorgenti.db, lambda store: store.recent_fills(n))
    return Elenco(False, nota) if righe is None else Elenco(True, "", tuple(righe))


def decisioni_rischio(
    sorgenti: Sorgenti, giorni: int = GIORNI_DECISIONI, oggi: date | None = None
) -> Finestra[DecisioneRischio]:
    """Decisioni del rischio negli ultimi `giorni` di calendario, oggi compreso.

    Le giornate sono quelle UTC del timestamp di log, come nel report settimanale.
    Le approvazioni pure sono loggate a DEBUG, quindi in live compaiono di solito solo
    riduzioni e rifiuti.
    """
    dal, al = _estremi(giorni, oggi)
    eventi, nota = _dal_log(sorgenti.log, dal, al)
    if eventi is None:
        return Finestra(False, nota, dal, al)
    return Finestra(True, "", dal, al, tuple(_decisione(e) for e in risk_decisions(eventi)))


def anomalie_recenti(
    sorgenti: Sorgenti, giorni: int = GIORNI_ANOMALIE, oggi: date | None = None
) -> Finestra[Anomalia]:
    """Anomalie di `quant.logreader.anomalies` negli ultimi `giorni`, oggi compreso."""
    dal, al = _estremi(giorni, oggi)
    eventi, nota = _dal_log(sorgenti.log, dal, al)
    if eventi is None:
        return Finestra(False, nota, dal, al)
    return Finestra(True, "", dal, al, tuple(_anomalia(e) for e in anomalies(eventi)))


def ritardo_esecuzione(sorgenti: Sorgenti, stato: StatoLive, oggi: date | None = None) -> Ritardo | None:
    """Sedute dopo l'ultima esecuzione nota fino a oggi compreso; None se non ce n'e' nessuna.

    L'ultima esecuzione e' la data piu' recente fra la passata nel log e l'equity
    salvata: un log finito altrove non deve far sembrare fermo un runner che gira.
    """
    candidati: list[tuple[date, str]] = []
    esecuzione = stato.ultima_esecuzione
    if esecuzione is not None and esecuzione.momento is not None:
        candidati.append((esecuzione.momento.astimezone(FUSO_BORSA).date(), "log"))
    if stato.giorno_equity is not None:
        candidati.append((stato.giorno_equity, "database"))
    if not candidati:
        return None
    ultima, fonte = max(candidati)
    try:
        sedute = giorni_di_borsa_dopo(ultima, oggi or oggi_in_borsa(), sorgenti.data_dir)
    except (OSError, ValueError) as errore:
        nota = f"calendario di borsa in {sorgenti.data_dir} non leggibile: {errore}"
        return Ritardo(ultima, fonte, (), nota)
    return Ritardo(ultima, fonte, tuple(sedute))


def performance(sorgenti: Sorgenti, ombra: ShadowResult | None, nota_ombra: str = "") -> Performance:
    """Confronto di `quant.weekly.confronto_live`, con lo StateStore aperto in sola lettura."""
    try:
        with apri_sola_lettura(sorgenti.db) as store:
            confronto = confronto_live(store, ombra, sorgenti.data_dir)
            nota = DB_ASSENTE if store is None else ""
    except (sqlite3.Error, OSError, ValueError) as errore:
        return Performance(None, f"confronto non calcolabile: {errore}", nota_ombra)
    return Performance(confronto, nota, nota_ombra)


def filtro_ordini(dal: str | None, al: str | None, simbolo: str | None, oggi: date) -> FiltroOrdini:
    """Filtro della pagina ordini dalla query string.

    Senza date si guardano gli ultimi trenta giorni. Un valore che non si legge si ignora
    e lo si dice nella pagina, invece di rispondere con un errore.
    """
    errori: list[str] = []
    inizio = _data(dal, "dal", errori) or oggi - timedelta(days=GIORNI_ORDINI - 1)
    fine = _data(al, "al", errori) or oggi
    codice = (simbolo or "").strip().upper()
    if codice and not SIMBOLO_VALIDO.fullmatch(codice):
        errori.append(f"simbolo {simbolo!r} non valido: filtro sul simbolo ignorato")
        codice = ""
    return FiltroOrdini(inizio, fine, codice or None, tuple(errori))


def giorni_finestra(testo: str | None, predefinito: int = GIORNI_DECISIONI) -> int:
    """Ampiezza della finestra dalla query string, fra 1 e 365 giorni; altrimenti il predefinito."""
    try:
        giorni = int(testo) if testo else predefinito
    except ValueError:
        return predefinito
    return giorni if 1 <= giorni <= 365 else predefinito


def ordini_e_eseguiti(
    sorgenti: Sorgenti, filtro: FiltroOrdini, ombra: ShadowResult | None, limite: int = RIGHE_ORDINI
) -> OrdiniEseguiti:
    """Ordini ed eseguiti nel filtro, ciascun eseguito con lo scarto dal fill teorico dello shadow.

    Appaiamento per giornata e simbolo e scarti in bps sono quelli di
    `quant.reconcile_report.compare`, lo stesso confronto del report settimanale.
    """

    def leggi(store: StateStore) -> tuple[list[StoredOrder], list[StoredFill], list[FillRecord], int]:
        return (
            store.find_orders(filtro.dal, filtro.al, filtro.simbolo, limite + 1),
            store.find_fills(filtro.dal, filtro.al, filtro.simbolo, limite + 1),
            real_fills(store),
            len(store.pending_orders()),
        )

    letto, nota = _dal_database(sorgenti.db, leggi)
    if letto is None:
        return OrdiniEseguiti(False, nota)
    ordini, eseguiti, reali, in_attesa = letto
    teorici = theoretical_fills(ombra) if ombra is not None else []
    try:
        aperture = reference_opens({f.symbol for f in [*reali, *teorici]}, sorgenti.data_dir)
    except (OSError, ValueError):
        aperture = {}
    per_chiave = {(c.day, c.symbol): c for c in compare(reali, teorici, aperture)}
    righe = tuple(
        RigaEseguito(f, per_chiave.get((datetime.fromisoformat(f.timestamp).date(), f.symbol)))
        for f in eseguiti[:limite]
    )
    solo_shadow = sorted(
        (c for c in per_chiave.values() if c.status == SOLO_SHADOW and filtro.comprende(c.day, c.symbol)),
        key=lambda c: c.day,
        reverse=True,
    )
    troncato = len(ordini) > limite or len(eseguiti) > limite
    return OrdiniEseguiti(True, "", tuple(ordini[:limite]), righe, tuple(solo_shadow), in_attesa, troncato)


def rischio(sorgenti: Sorgenti, giorni: int = GIORNI_DECISIONI, oggi: date | None = None) -> Rischio:
    """Decisioni del rischio raggruppate con `quant.logreader.decisions_by_reason` e anomalie."""
    dal, al = _estremi(giorni, oggi)
    eventi, nota = _dal_log(sorgenti.log, dal, al)
    if eventi is None:
        return Rischio(False, nota, dal, al)
    gruppi = [
        GruppoMotivo(motivo, tuple(_decisione(e) for e in decisioni))
        for motivo, decisioni in decisions_by_reason(eventi).items()
    ]
    gruppi.sort(key=lambda gruppo: len(gruppo.decisioni), reverse=True)
    return Rischio(
        True,
        "",
        dal,
        al,
        decisioni=tuple(_decisione(e) for e in risk_decisions(eventi)),
        gruppi=tuple(gruppi),
        anomalie=tuple(_anomalia(e) for e in anomalies(eventi)),
    )


def tipo_report(nome: str | Path) -> str:
    """Testo per markdown, CSV e simili, immagine per i PNG, altro per tutto il resto."""
    suffisso = Path(nome).suffix.lower()
    if suffisso in TIPI_IMMAGINE:
        return TIPO_IMMAGINE
    return TIPO_TESTO if suffisso in SUFFISSI_TESTO else TIPO_ALTRO


def risolvi_report(cartella: Path, nome: str) -> Path | None:
    """File richiesto dentro la cartella dei report; None se ne esce, e' nascosto o non esiste.

    Il percorso si risolve, link simbolici compresi, e deve restare sotto la cartella
    risolta: `..`, percorsi assoluti e link verso l'esterno danno tutti None.
    """
    if not nome or "\x00" in nome:
        return None
    relativo = Path(nome)
    if relativo.is_absolute() or any(parte.startswith(".") for parte in relativo.parts):
        return None
    base = cartella.resolve()
    try:
        candidato = (base / relativo).resolve()
    except (OSError, RuntimeError):
        return None
    if not candidato.is_relative_to(base) or not candidato.is_file():
        return None
    return candidato


def elenco_report(sorgenti: Sorgenti) -> Elenco[FileReport]:
    """File in `reports/`, dal piu' recente; nascosti e link che escono dalla cartella restano fuori."""
    cartella = sorgenti.reports_dir
    if not cartella.is_dir():
        nota = f"cartella {cartella} assente: la creano il report settimanale e gli script di ricerca"
        return Elenco(False, nota)
    base = cartella.resolve()
    righe: list[FileReport] = []
    try:
        for file in sorted(base.rglob("*")):
            nome = file.relative_to(base).as_posix()
            percorso = risolvi_report(base, nome)
            if percorso is None:
                continue
            informazioni = percorso.stat()
            modificato = datetime.fromtimestamp(informazioni.st_mtime)
            righe.append(FileReport(nome, modificato, informazioni.st_size))
            if len(righe) >= MASSIMO_FILE_REPORT:
                break
    except OSError as errore:
        return Elenco(False, f"cartella {cartella} non leggibile: {errore}")
    righe.sort(key=lambda file: file.modificato, reverse=True)
    return Elenco(True, "", tuple(righe))


def leggi_testo(percorso: Path) -> tuple[str, bool]:
    """Contenuto di un report di testo, troncato oltre `MASSIMO_TESTO` byte."""
    with percorso.open("rb") as file:
        dati = file.read(MASSIMO_TESTO + 1)
    return dati[:MASSIMO_TESTO].decode("utf-8", errors="replace"), len(dati) > MASSIMO_TESTO


def immagine_collegata(cartella: Path, percorso: Path) -> str | None:
    """Immagine con lo stesso nome del report, come il PNG che accompagna il markdown settimanale."""
    for suffisso in TIPI_IMMAGINE:
        vicina = percorso.with_suffix(suffisso)
        if vicina.is_file():
            return vicina.relative_to(cartella.resolve()).as_posix()
    return None


def ombra_del_live(giorno: date, dati: Path) -> ShadowResult:
    """Lo shadow di `quant.weekly.shadow_live`: strategia e universo del live, dall'avvio a `giorno`."""
    return shadow_live(giorno, dati=dati)


def impronta_parquet(cartella: Path) -> tuple[tuple[str, int, int], ...]:
    """Nome, dimensione e ultima modifica di ogni Parquet: cambia quando cambiano i dati."""
    if not cartella.is_dir():
        return ()
    return tuple(sorted((f.name, f.stat().st_size, f.stat().st_mtime_ns) for f in cartella.glob("*.parquet")))


class OmbraMemorizzata:
    """Shadow rigiocato solo quando cambiano la giornata o i Parquet.

    La pagina si ricarica ogni minuto e un backtest costa secondi, mentre il risultato
    dipende solo da questi due fattori. Anche un fallimento resta memorizzato come nota,
    cosi' non si ritenta a ogni ricarica: si riprova quando arrivano dati nuovi.
    """

    def __init__(self, calcola: CalcoloOmbra, dati: Path) -> None:
        self.calcola = calcola
        self.dati = dati
        self._blocco = threading.Lock()
        self._chiave: tuple[object, ...] | None = None
        self._esito: tuple[ShadowResult | None, str] = (None, "")

    def __call__(self, giorno: date) -> tuple[ShadowResult | None, str]:
        """Shadow e nota della giornata, ricalcolati solo se la chiave e' cambiata."""
        chiave = (giorno, impronta_parquet(self.dati))
        with self._blocco:
            if chiave != self._chiave:
                self._esito = self._calcola(giorno)
                self._chiave = chiave
            return self._esito

    def _calcola(self, giorno: date) -> tuple[ShadowResult | None, str]:
        """Un guasto del backtest diventa una nota, mai un'eccezione nella pagina."""
        try:
            return self.calcola(giorno, self.dati), ""
        except Exception as errore:
            return None, f"shadow non disponibile: {type(errore).__name__}: {errore}"


def _data(testo: str | None, nome: str, errori: list[str]) -> date | None:
    """Data ISO dalla query string; un valore illeggibile finisce fra gli errori."""
    if not testo:
        return None
    try:
        return date.fromisoformat(testo)
    except ValueError:
        errori.append(f"data {nome}={testo!r} non valida: uso il valore predefinito")
        return None


def _estremi(giorni: int, oggi: date | None) -> tuple[date, date]:
    """Primo e ultimo giorno di una finestra di `giorni` che finisce oggi."""
    al = oggi or oggi_in_borsa()
    return al - timedelta(days=max(giorni, 1) - 1), al


def _dal_database[R](db: Path, lettura: Callable[[StateStore], R]) -> tuple[R | None, str]:
    """Esegue una lettura; se il database manca o non si legge, None e il motivo."""
    try:
        with apri_sola_lettura(db) as store:
            if store is None:
                return None, DB_ASSENTE
            return lettura(store), ""
    except sqlite3.Error as errore:
        return None, f"database {db} non leggibile: {errore}"


def _dal_log(log: Path, dal: date | None = None, al: date | None = None) -> tuple[list[Evento] | None, str]:
    """Eventi del log nella finestra; se il file manca o non si legge, None e il motivo."""
    if not log.is_file():
        return None, f"log {log} assente: lo crea lo scheduler (scripts/live.py) all'avvio"
    try:
        return read_events(log, since=dal, until=al), ""
    except (OSError, UnicodeDecodeError) as errore:
        return None, f"log {log} non leggibile: {errore}"


def _ultima_esecuzione(log: Path) -> tuple[UltimaEsecuzione | None, str]:
    """Ultima passata conclusa o fallita secondo il log, con la nota se non ce n'e'."""
    eventi, nota = _dal_log(log)
    if eventi is None:
        return None, nota
    evento = last_run(eventi)
    if evento is None:
        return None, f"nessuna esecuzione conclusa o fallita in {log}"
    return UltimaEsecuzione(event_time(evento), _testo(evento.get("run_id")), str(evento.get("event"))), ""


def _decisione(evento: Evento) -> DecisioneRischio:
    """Riga di log del RiskManager in forma tipizzata."""
    return DecisioneRischio(
        momento=event_time(evento),
        evento=str(evento.get("event", "")),
        symbol=str(evento.get("symbol", "")),
        quantita_richiesta=_intero(evento.get("quantity_original")),
        quantita_finale=_intero(evento.get("quantity_final")),
        motivo=str(evento.get("reason", "")),
        run_id=_testo(evento.get("run_id")),
    )


def _anomalia(evento: Evento) -> Anomalia:
    """Riga di log anomala in forma tipizzata; lo stack trace resta nel log."""
    campi = tuple(
        (chiave, "-" if valore is None else str(valore))
        for chiave, valore in evento.items()
        if chiave not in CAMPI_DI_SERVIZIO
    )
    return Anomalia(
        momento=event_time(evento),
        evento=str(evento.get("event", "")),
        symbol=_testo(evento.get("symbol")),
        run_id=_testo(evento.get("run_id")),
        campi=campi,
    )


def _intero(valore: object) -> int | None:
    """Intero se il campo lo e', altrimenti None."""
    return valore if isinstance(valore, int) and not isinstance(valore, bool) else None


def _testo(valore: object) -> str | None:
    """Stringa se il campo lo e', altrimenti None."""
    return valore if isinstance(valore, str) else None
