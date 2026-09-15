"""Avvio della dashboard: solo su loopback, log in console, nessun file del live toccato.

Al caricamento questo modulo non importa niente che crei logger. Con `QUANT_LOG_FILE`
impostato, il primo logger aprirebbe in scrittura il log del live e la dashboard ci
scriverebbe le sue righe: `prepara_log` toglie la variabile prima che il resto venga
importato, e il percorso resta solo come file da leggere.
"""

from __future__ import annotations

import ipaddress
import os
import sys
from pathlib import Path

from quant.logging import VARIABILE_FILE, configure

HOST_PREDEFINITO = "127.0.0.1"
PORTA_PREDEFINITA = 8000
LOCALHOST = "localhost"
CODICE_RIFIUTO = 2
PERCHE = (
    "La dashboard non ha autenticazione: in ascolto su un indirizzo raggiungibile da fuori, "
    "chiunque arrivi alla macchina vedrebbe posizioni, equity e ordini. Per guardarla da un altro "
    "computer si usa un tunnel SSH verso 127.0.0.1, per esempio ssh -L 8000:127.0.0.1:8000 utente@macchina."
)


class HostNonAmmesso(ValueError):
    """Indirizzo di ascolto rifiutato, con la spiegazione del perche'."""


def verifica_loopback(host: str) -> str:
    """Restituisce l'host se e' `localhost` o un indirizzo IPv4 di loopback, altrimenti solleva."""
    if host == LOCALHOST:
        return host
    try:
        indirizzo = ipaddress.ip_address(host)
    except ValueError as errore:
        raise HostNonAmmesso(f"{host!r} non e' un indirizzo IP ne' localhost. {PERCHE}") from errore
    if not indirizzo.is_loopback:
        raise HostNonAmmesso(f"{host} non e' un indirizzo di loopback. {PERCHE}")
    if indirizzo.version != 4:
        raise HostNonAmmesso(
            f"{host} e' loopback IPv6: usare 127.0.0.1, perche' il controllo dell'header Host "
            "che protegge dal DNS rebinding e' scritto per IPv4."
        )
    return host


def prepara_log() -> None:
    """Log della dashboard in console: il file del live lo scrive soltanto il live."""
    os.environ.pop(VARIABILE_FILE, None)
    configure(force=True)


def avvia(
    host: str = HOST_PREDEFINITO,
    porta: int = PORTA_PREDEFINITA,
    db: str | Path | None = None,
    dati: str | Path | None = None,
    report: str | Path | None = None,
    log: str | Path | None = None,
) -> int:
    """Controlla l'host, prepara i log, avvia uvicorn e restituisce il codice di uscita.

    Senza `log` si legge il file che il live scrive: quello di `QUANT_LOG_FILE` se e'
    impostato, altrimenti `logs/live.jsonl`. Gli altri percorsi mancanti restano quelli
    predefiniti del progetto.
    """
    try:
        host = verifica_loopback(host)
    except HostNonAmmesso as errore:
        print(f"Avvio rifiutato: {errore}", file=sys.stderr)
        return CODICE_RIFIUTO
    log_del_live = log or os.environ.get(VARIABILE_FILE) or None
    prepara_log()

    import uvicorn

    from quant.config import PERCORSO_DATI
    from quant.logreader import PERCORSO_LOG
    from quant.state import PERCORSO_DB
    from quant.webui.app import HOST_AMMESSI, create_app
    from quant.weekly import REPORT

    app = create_app(
        db=db or PERCORSO_DB,
        data_dir=dati or PERCORSO_DATI,
        reports_dir=report or REPORT,
        log=log_del_live or PERCORSO_LOG,
        host_ammessi=(*HOST_AMMESSI, host),
    )
    print(f"Dashboard di sola lettura su http://{host}:{porta}/  (Ctrl+C per fermarla)", flush=True)
    uvicorn.run(app, host=host, port=porta, proxy_headers=False, server_header=False, log_level="warning")
    return 0
