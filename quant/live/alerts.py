"""Notifiche verso Telegram, opzionali: senza credenziali resta solo il log."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from quant.config import Settings, load_settings
from quant.logging import get_logger

logger = get_logger("live.alerts")
TIMEOUT = 10
API = "https://api.telegram.org/bot{token}/sendMessage"


class NullAlerter:
    """Alerter inerte: usato quando le variabili d'ambiente non ci sono."""

    def send(self, level: str, message: str) -> bool:
        """Registra soltanto, senza uscire dal processo."""
        logger.info("alert_non_inviato", level=level, message=message)
        return False


class TelegramAlerter:
    """Manda messaggi a una chat Telegram; un errore di rete non deve fermare il trading."""

    def __init__(self, bot_token: str, chat_id: str, prefix: str = "quant") -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.prefix = prefix

    def send(self, level: str, message: str) -> bool:
        """Invia il messaggio; False se la chiamata fallisce, senza sollevare."""
        testo = f"[{self.prefix}][{level.upper()}] {message}"
        dati = urllib.parse.urlencode({"chat_id": self.chat_id, "text": testo}).encode("utf-8")
        richiesta = urllib.request.Request(API.format(token=self.bot_token), data=dati)
        try:
            with urllib.request.urlopen(richiesta, timeout=TIMEOUT) as risposta:
                esito = json.loads(risposta.read().decode("utf-8"))
        except Exception as errore:  # noqa: BLE001 - l'alert non deve mai propagare
            logger.warning("alert_fallito", level=level, errore=str(errore))
            return False
        if not esito.get("ok", False):
            logger.warning("alert_rifiutato", level=level, risposta=esito)
            return False
        logger.info("alert_inviato", level=level)
        return True


def build_alerter(settings: Settings | None = None) -> Any:
    """Alerter Telegram se configurato, altrimenti quello inerte."""
    impostazioni = settings or load_settings()
    if impostazioni.telegram_enabled:
        return TelegramAlerter(impostazioni.telegram_bot_token, impostazioni.telegram_chat_id)
    logger.info("alert_disabilitati")
    return NullAlerter()
