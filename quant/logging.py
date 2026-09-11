"""Logging strutturato: JSON su file in produzione, console leggibile in sviluppo."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, TextIO

import structlog

VARIABILE_FILE = "QUANT_LOG_FILE"
VARIABILE_LIVELLO = "QUANT_LOG_LEVEL"
PERCORSO_LOG_LIVE = Path("logs/live.jsonl")

_configurato = False
_destinazione: TextIO | None = None


def _livello_da_ambiente(default: int) -> int:
    """Livello di log preso dall'ambiente, con ricaduta sul default."""
    nome = os.environ.get(VARIABILE_LIVELLO, "").upper()
    livello = getattr(logging, nome, None) if nome else None
    return livello if isinstance(livello, int) else default


def _chiudi_destinazione() -> None:
    """Chiude il file di log corrente, lasciando stare standard error."""
    global _destinazione
    if _destinazione is not None and _destinazione is not sys.stderr:
        _destinazione.close()
    _destinazione = None


def configure(
    level: int = logging.INFO,
    log_file: str | Path | None = None,
    json_output: bool | None = None,
    force: bool = False,
) -> None:
    """Configura structlog per l'intero processo.

    Con un file di log, o con la variabile d'ambiente `QUANT_LOG_FILE`, si scrive JSON
    una riga per evento: e' il formato che il report settimanale sa rileggere. Senza
    file si stampa su standard error in forma leggibile, che serve solo in sviluppo.
    I logger non vengono messi in cache: una riconfigurazione all'avvio vale anche per
    quelli creati all'import dei moduli.
    """
    global _configurato, _destinazione
    if _configurato and not force:
        return
    _chiudi_destinazione()

    percorso = log_file or os.environ.get(VARIABILE_FILE) or None
    if percorso:
        file = Path(percorso)
        file.parent.mkdir(parents=True, exist_ok=True)
        _destinazione = file.open("a", encoding="utf-8", buffering=1)
    else:
        _destinazione = sys.stderr

    usa_json = json_output if json_output is not None else bool(percorso)
    renderer: Any = structlog.processors.JSONRenderer() if usa_json else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_livello_da_ambiente(level)),
        logger_factory=structlog.PrintLoggerFactory(file=_destinazione),
        cache_logger_on_first_use=False,
    )
    _configurato = True


def configure_for_live(log_file: str | Path | None = None) -> Path:
    """Configurazione del processo live: JSON su file, per default `logs/live.jsonl`.

    Il report settimanale legge le decisioni del rischio e le anomalie da questo file,
    quindi il live non puo' dipendere da un redirect ricordato a mano.
    """
    percorso = Path(log_file or os.environ.get(VARIABILE_FILE) or PERCORSO_LOG_LIVE)
    configure(log_file=percorso, json_output=True, force=True)
    return percorso


def get_logger(name: str) -> Any:
    """Logger strutturato legato a un componente."""
    configure()
    return structlog.get_logger(name)


def reset_for_tests() -> None:
    """Riporta la configurazione di sviluppo: serve ai test che cambiano destinazione."""
    global _configurato
    _chiudi_destinazione()
    _configurato = False
    structlog.reset_defaults()
    configure()
