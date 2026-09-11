"""Test degli alert: opzionali, silenziosi in caso di errore."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from quant.config import Settings
from quant.live import alerts
from quant.live.alerts import NullAlerter, TelegramAlerter, build_alerter


class RispostaFinta:
    """Risposta HTTP finta, usabile come context manager."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def __enter__(self) -> RispostaFinta:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_senza_credenziali_si_usa_lalerter_inerte() -> None:
    alerter = build_alerter(Settings(telegram_bot_token="", telegram_chat_id=""))
    assert isinstance(alerter, NullAlerter)
    assert alerter.send("info", "messaggio") is False


def test_con_credenziali_si_usa_telegram() -> None:
    alerter = build_alerter(Settings(telegram_bot_token="t", telegram_chat_id="c"))
    assert isinstance(alerter, TelegramAlerter)


def test_invio_riuscito(monkeypatch) -> None:
    inviati: list[Any] = []

    def finta_urlopen(request: Any, timeout: int = 0) -> RispostaFinta:
        inviati.append(request)
        return RispostaFinta({"ok": True})

    monkeypatch.setattr(alerts.urllib.request, "urlopen", finta_urlopen)
    assert TelegramAlerter("token", "chat").send("warning", "attenzione") is True
    assert "bottoken" in inviati[0].full_url
    assert b"attenzione" in inviati[0].data


def test_errore_di_rete_non_propaga(monkeypatch) -> None:
    def esplode(request: Any, timeout: int = 0) -> None:
        raise OSError("rete assente")

    monkeypatch.setattr(alerts.urllib.request, "urlopen", esplode)
    assert TelegramAlerter("token", "chat").send("error", "problema") is False


def test_risposta_negativa_del_servizio(monkeypatch) -> None:
    monkeypatch.setattr(
        alerts.urllib.request,
        "urlopen",
        lambda request, timeout=0: RispostaFinta({"ok": False, "description": "chat non trovata"}),
    )
    assert TelegramAlerter("token", "chat").send("info", "ciao") is False


def test_il_runner_avvisa_su_ordini_e_scarti(tmp_path, momentum_parquet_dir) -> None:
    """Gli alert seguono le fasi del runner senza poterne fermare l'esecuzione."""
    from test_live_runner import costruisci

    ricevuti: list[tuple[str, str]] = []
    runner, _ = costruisci(tmp_path, momentum_parquet_dir, [], positions={"ZZZ": 5})
    runner.alerter = SimpleNamespace(send=lambda level, message: ricevuti.append((level, message)))
    runner.run_once()

    livelli = [livello for livello, _ in ricevuti]
    assert "info" in livelli
    assert any("RECONCILIATION_MISMATCH" in messaggio for _, messaggio in ricevuti)
    assert "error" in livelli
