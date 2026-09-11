"""Lettura dei log JSON prodotti dal live: sola analisi, nessuna scrittura."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from quant.logging import PERCORSO_LOG_LIVE

PERCORSO_LOG = PERCORSO_LOG_LIVE

DECISIONI_RISCHIO = ("ordine_approvato", "ordine_ridotto", "ordine_rifiutato")
MISMATCH = "RECONCILIATION_MISMATCH"
ECCEZIONI = ("run_fallita", "kill_switch_attivato")


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


def event_day(evento: dict[str, Any]) -> date | None:
    """Giornata di un evento, ricavata dal timestamp ISO."""
    grezzo = evento.get("timestamp")
    if not isinstance(grezzo, str):
        return None
    try:
        return datetime.fromisoformat(grezzo.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def by_event(eventi: Iterable[dict[str, Any]], nomi: Sequence[str]) -> list[dict[str, Any]]:
    """Sottoinsieme degli eventi con uno dei nomi indicati."""
    ammessi = set(nomi)
    return [e for e in eventi if e.get("event") in ammessi]


def risk_decisions(eventi: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Decisioni del gestore del rischio registrate nel periodo."""
    return by_event(eventi, DECISIONI_RISCHIO)


def mismatches(eventi: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Disallineamenti di riconciliazione."""
    return by_event(eventi, [MISMATCH])


def failures(eventi: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Eccezioni e attivazioni del kill switch."""
    return by_event(eventi, ECCEZIONI)


def run_days(eventi: Iterable[dict[str, Any]]) -> set[date]:
    """Giornate in cui il runner ha effettivamente concluso una passata."""
    giorni = set()
    for evento in by_event(eventi, ["run_conclusa"]):
        giorno = event_day(evento)
        if giorno is not None:
            giorni.add(giorno)
    return giorni
