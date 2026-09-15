"""Avvisi in cima alla dashboard: cosa e' successo e dove guardare, non solo che qualcosa non va."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from quant.logreader import MISMATCH, RUN_FALLITA
from quant.webui.formato import momento_borsa
from quant.webui.read import DB_ASSENTE, Anomalia, Finestra, Ritardo, Sorgenti, StatoLive

ALLARME = "allarme"
INFO = "info"
SOGLIA_SEDUTE = 2
MISMATCH_ELENCATI = 5


@dataclass(frozen=True, slots=True)
class Avviso:
    """Riga del banner: gravita', cosa e' successo, dove guardare."""

    livello: str
    titolo: str
    dettaglio: str
    dove: str


def avvisi(
    stato: StatoLive, anomalie: Finestra[Anomalia], ritardo: Ritardo | None, sorgenti: Sorgenti
) -> tuple[Avviso, ...]:
    """Tutti gli avvisi della pagina, prima gli allarmi e poi le note."""
    elenco = [
        *_kill_switch(stato),
        *_database(stato, sorgenti),
        *_ritardo(ritardo, sorgenti),
        *_fallite(anomalie, sorgenti),
        *_disallineamenti(anomalie, sorgenti),
        *_log(stato),
    ]
    return tuple(sorted(elenco, key=lambda avviso: avviso.livello != ALLARME))


def _kill_switch(stato: StatoLive) -> list[Avviso]:
    """Interruttore attivo: nessun ordine esce finche' il file esiste."""
    interruttore = stato.kill_switch
    if not interruttore.attivo:
        return []
    return [
        Avviso(
            ALLARME,
            "Kill switch attivo: il live non invia ordini",
            f"Motivo scritto nel file: {interruttore.motivo or 'nessuno'}. Finche' {interruttore.percorso} "
            "esiste il RiskManager rifiuta ogni ordine, anche quelli che ridurrebbero una posizione.",
            "le righe run_fallita e kill_switch_attivato nel log del live. Il file si rimuove a mano da "
            "riga di comando, dopo aver capito la causa: la dashboard non lo tocca.",
        )
    ]


def _database(stato: StatoLive, sorgenti: Sorgenti) -> list[Avviso]:
    """Database assente prima del primo run, o presente ma illeggibile."""
    if stato.disponibile:
        return []
    if stato.nota == DB_ASSENTE:
        return [
            Avviso(
                INFO,
                "Nessuna esecuzione live registrata",
                f"{sorgenti.db} non esiste ancora.",
                "scripts/live.py: la pagina si popola dalla prima esecuzione dello scheduler.",
            )
        ]
    return [
        Avviso(
            ALLARME,
            "Database live non leggibile",
            stato.nota,
            f"il file {sorgenti.db} e l'ultimo run_fallita nel log. La dashboard lo apre in sola lettura "
            "e non prova a ripararlo.",
        )
    ]


def _ritardo(ritardo: Ritardo | None, sorgenti: Sorgenti) -> list[Avviso]:
    """Runner fermo da piu' di due giornate di borsa."""
    if ritardo is None:
        return []
    if ritardo.nota:
        return [
            Avviso(
                INFO,
                "Ritardo del runner non verificabile",
                ritardo.nota,
                f"{sorgenti.data_dir / 'SPY.parquet'}, controllabile con scripts/download.py --check.",
            )
        ]
    if len(ritardo.sedute) <= SOGLIA_SEDUTE:
        return []
    return [
        Avviso(
            ALLARME,
            f"Nessuna esecuzione da {len(ritardo.sedute)} giornate di borsa",
            f"Ultima esecuzione nota il {ritardo.ultima} (fonte: {ritardo.fonte}). "
            f"Giornate di borsa successive: {_elenco(ritardo.sedute)}.",
            f"che scripts/live.py sia in esecuzione, e la coda di {sorgenti.log}. Il calendario viene da "
            f"{sorgenti.data_dir / 'SPY.parquet'}; oltre l'ultima barra salvata si contano i feriali.",
        )
    ]


def _fallite(anomalie: Finestra[Anomalia], sorgenti: Sorgenti) -> list[Avviso]:
    """Esecuzioni finite con un'eccezione nella finestra."""
    fallite = [a for a in anomalie.righe if a.evento == RUN_FALLITA]
    if not fallite:
        return []
    ultima = fallite[-1]
    return [
        Avviso(
            ALLARME,
            f"Esecuzioni fallite dal {anomalie.dal}: {len(fallite)}",
            f"L'ultima il {momento_borsa(ultima.momento)}, run_id {ultima.run_id or '-'}: "
            f"{ultima.campo('errore') or 'errore non registrato'}.",
            f"{sorgenti.log}, righe run_fallita con quel run_id: lo stack trace e' nel campo exception. "
            "Il runner non ritenta da solo.",
        )
    ]


def _disallineamenti(anomalie: Finestra[Anomalia], sorgenti: Sorgenti) -> list[Avviso]:
    """Posizioni attese diverse da quelle del broker nella finestra."""
    righe = [a for a in anomalie.righe if a.evento == MISMATCH]
    if not righe:
        return []
    descritti = "; ".join(
        f"{a.symbol or '?'} attese {a.campo('expected') or '?'}, reali {a.campo('actual') or '?'} "
        f"il {momento_borsa(a.momento)}"
        for a in righe[-MISMATCH_ELENCATI:]
    )
    return [
        Avviso(
            ALLARME,
            f"RECONCILIATION_MISMATCH dal {anomalie.dal}: {len(righe)}",
            f"{descritti}.",
            f"le posizioni attese qui sotto contro quelle del broker, e {sorgenti.log}. "
            "Il sistema segnala e non corregge.",
        )
    ]


def _log(stato: StatoLive) -> list[Avviso]:
    """Log assente o senza passate: senza, esecuzioni e anomalie non si vedono."""
    if not stato.nota_log:
        return []
    return [
        Avviso(
            INFO,
            "Esecuzioni non visibili nel log",
            stato.nota_log,
            "lo scheduler scrive in logs/live.jsonl, o nel percorso di QUANT_LOG_FILE: senza quel file "
            "mancano esecuzioni, decisioni del rischio e anomalie.",
        )
    ]


def _elenco(giorni: Sequence[date]) -> str:
    """Elenco compatto di date."""
    return ", ".join(str(g) for g in giorni[:8]) + (" ..." if len(giorni) > 8 else "")
