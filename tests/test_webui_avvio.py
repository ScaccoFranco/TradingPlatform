"""Avvio della dashboard: solo loopback, e nessuna riga nel log del live."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient

from quant.webui.avvio import CODICE_RIFIUTO, HostNonAmmesso, avvia, verifica_loopback
from quant.webui.read import Sorgenti

RADICE = Path(__file__).resolve().parents[1]
PREPARAZIONE = "from quant.webui.avvio import prepara_log\nprepara_log()\n"
DASHBOARD_CHE_LOGGA = (
    "import quant.webui.app\nfrom quant.logging import get_logger\nget_logger('ui').warning('x')\n"
)


def test_solo_loopback_ipv4_o_localhost() -> None:
    for host in ("127.0.0.1", "127.0.0.2", "localhost"):
        assert verifica_loopback(host) == host
    for host in ("0.0.0.0", "192.168.1.10", "10.0.0.5", "::", "example.com", ""):
        with pytest.raises(HostNonAmmesso, match="non ha autenticazione"):
            verifica_loopback(host)
    with pytest.raises(HostNonAmmesso, match="IPv6"):
        verifica_loopback("::1")


def test_host_esterno_rifiutato_prima_di_avviare(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(uvicorn, "run", lambda *_, **__: pytest.fail("uvicorn non doveva partire"))
    assert avvia("0.0.0.0") == CODICE_RIFIUTO
    errore = capsys.readouterr().err
    assert errore.startswith("Avvio rifiutato: 0.0.0.0 non e' un indirizzo di loopback.")
    assert "ssh -L 8000:127.0.0.1:8000" in errore


def test_avvio_su_loopback_con_i_percorsi_indicati(
    sorgenti: Sorgenti, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    chiamata: dict[str, Any] = {}

    def finto(app: Any, **opzioni: Any) -> None:
        chiamata.update(opzioni, app=app)

    monkeypatch.setattr(uvicorn, "run", finto)
    # La riconfigurazione dei log e' provata nel suo processo, qui toccherebbe lo stderr catturato.
    monkeypatch.setattr("quant.webui.avvio.prepara_log", lambda: None)
    esito = avvia(
        "127.0.0.2",
        8123,
        db=sorgenti.db,
        dati=sorgenti.data_dir,
        report=sorgenti.reports_dir,
        log=sorgenti.log,
    )

    assert esito == 0
    assert (chiamata["host"], chiamata["port"], chiamata["proxy_headers"]) == ("127.0.0.2", 8123, False)
    assert "Dashboard di sola lettura su http://127.0.0.2:8123/" in capsys.readouterr().out
    pagina = TestClient(chiamata["app"], base_url="http://127.0.0.2").get("/")
    assert pagina.status_code == 200 and str(sorgenti.db) in pagina.text
    assert not sorgenti.db.exists()


def test_lo_script_rifiuta_un_host_esterno() -> None:
    esito = subprocess.run(
        [sys.executable, "scripts/ui.py", "--host", "0.0.0.0"], cwd=RADICE, capture_output=True, text=True
    )
    assert esito.returncode == CODICE_RIFIUTO
    assert "Avvio rifiutato" in esito.stderr and "non ha autenticazione" in esito.stderr


def test_la_dashboard_non_scrive_nel_log_del_live(tmp_path: Path) -> None:
    """Con QUANT_LOG_FILE impostato, senza la preparazione i log finirebbero nel file del live."""
    for nome, prima in (("senza", ""), ("con", PREPARAZIONE)):
        log = tmp_path / nome / "live.jsonl"
        ambiente = {**os.environ, "QUANT_LOG_FILE": str(log)}
        subprocess.run([sys.executable, "-c", prima + DASHBOARD_CHE_LOGGA], env=ambiente, check=True)
        assert log.exists() == (nome == "senza"), nome
