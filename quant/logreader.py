"""Lettura dei log JSON prodotti dal live: sola analisi, nessuna scrittura."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from quant.logging import PERCORSO_LOG_LIVE

PERCORSO_LOG = PERCORSO_LOG_LIVE

ORDINE_APPROVATO = "ordine_approvato"
ORDINE_RIDOTTO = "ordine_ridotto"
ORDINE_RIFIUTATO = "ordine_rifiutato"
DECISIONI_RISCHIO = (ORDINE_APPROVATO, ORDINE_RIDOTTO, ORDINE_RIFIUTATO)
SEPARATORE_MOTIVI = "+"
SENZA_MOTIVO = "SENZA_MOTIVO"
MISMATCH = "RECONCILIATION_MISMATCH"
RUN_CONCLUSA = "run_conclusa"
RUN_FALLITA = "run_fallita"
ESITI_RUN = (RUN_CONCLUSA, RUN_FALLITA)
KILL_SWITCH_ATTIVATO = "kill_switch_attivato"
POSIZIONE_SENZA_BARRE = "posizione_senza_barre"
ECCEZIONI = (RUN_FALLITA, KILL_SWITCH_ATTIVATO)
ANOMALIE = (RUN_FALLITA, KILL_SWITCH_ATTIVATO, MISMATCH, POSIZIONE_SENZA_BARRE)


def read_events(
    path: str | Path = PERCORSO_LOG,
    since: date | None = None,
    until: date | None = None,
) -> list[dict[str, Any]]:
    """Righe JSON del log, filtrate per giornata.

    Il file non esiste finche' lo scheduler non ha girato almeno una volta: in quel
    caso si restituisce una lista vuota, perche' l'assenza di log non deve far fallire
    un report.
    """
    file = Path(path)
    if not file.exists():
        return []
    eventi: list[dict[str, Any]] = []
    with file.open(encoding="utf-8") as sorgente:
        for riga in sorgente:
            riga = riga.strip()
            if not riga.startswith("{"):
                continue
            try:
                evento = json.loads(riga)
            except json.JSONDecodeError:
                continue
            giorno = event_day(evento)
            if giorno is None or (since and giorno < since) or (until and giorno > until):
                continue
            eventi.append(evento)
    return eventi


def event_time(evento: dict[str, Any]) -> datetime | None:
    """Istante di un evento, ricavato dal timestamp ISO."""
    grezzo = evento.get("timestamp")
    if not isinstance(grezzo, str):
        return None
    try:
        return datetime.fromisoformat(grezzo.replace("Z", "+00:00"))
    except ValueError:
        return None


def event_day(evento: dict[str, Any]) -> date | None:
    """Giornata di un evento, ricavata dal timestamp ISO."""
    momento = event_time(evento)
    return momento.date() if momento is not None else None


def by_event(eventi: Iterable[dict[str, Any]], nomi: Sequence[str]) -> list[dict[str, Any]]:
    """Sottoinsieme degli eventi con uno dei nomi indicati."""
    ammessi = set(nomi)
    return [e for e in eventi if e.get("event") in ammessi]


def risk_decisions(eventi: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Decisioni del gestore del rischio registrate nel periodo."""
    return by_event(eventi, DECISIONI_RISCHIO)


def split_reasons(reason: str) -> tuple[str, ...]:
    """Motivi RiskReason di una decisione: `RiskManager.decide` li unisce con '+'."""
    return tuple(parte for parte in reason.split(SEPARATORE_MOTIVI) if parte) or (SENZA_MOTIVO,)


def decisions_by_reason(eventi: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Decisioni del rischio raggruppate per motivo, nell'ordine della prima comparsa.

    Una decisione con due motivi, per esempio ridotta per peso e per controvalore,
    compare in entrambi i gruppi: ogni gruppo dice quante volte quel limite ha agito.
    """
    gruppi: dict[str, list[dict[str, Any]]] = {}
    for evento in risk_decisions(eventi):
        for motivo in split_reasons(str(evento.get("reason", ""))):
            gruppi.setdefault(motivo, []).append(evento)
    return gruppi


def mismatches(eventi: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Disallineamenti di riconciliazione."""
    return by_event(eventi, [MISMATCH])


def failures(eventi: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Eccezioni e attivazioni del kill switch."""
    return by_event(eventi, ECCEZIONI)


def anomalies(eventi: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Eventi che chiedono una persona: eccezioni, kill switch, disallineamenti, posizioni ferme."""
    return by_event(eventi, ANOMALIE)


def run_days(eventi: Iterable[dict[str, Any]]) -> set[date]:
    """Giornate in cui il runner ha effettivamente concluso una passata."""
    giorni = set()
    for evento in by_event(eventi, [RUN_CONCLUSA]):
        giorno = event_day(evento)
        if giorno is not None:
            giorni.add(giorno)
    return giorni


def last_run(eventi: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Ultima passata del runner arrivata in fondo, conclusa o fallita.

    Il log e' append-only, quindi l'ultima in ordine di scrittura e' anche la piu'
    recente: non serve confrontare timestamp che potrebbero mancare.
    """
    esiti = by_event(eventi, ESITI_RUN)
    return esiti[-1] if esiti else None
