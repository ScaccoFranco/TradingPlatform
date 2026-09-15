"""Dashboard via TestClient: pagine Stato e Performance, banner, rotte e intestazioni."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest
from conftest import scrivi_serie
from fastapi.testclient import TestClient
from sintetici import accoda_log, scrivi_log
from starlette.routing import Mount

from quant.events import FillEvent, OrderDirection, OrderEvent
from quant.shadow import ShadowResult
from quant.state import StateStore
from quant.webui.app import create_app
from quant.webui.read import DB_ASSENTE, CalcoloOmbra, Sorgenti

OGGI = date(2026, 9, 11)
LOOPBACK = "http://127.0.0.1"
SETTIMANA = pd.bdate_range("2026-09-07", periods=5)
EQUITY_LIVE = [100_000.0, 102_000.0, 99_960.0, 101_000.0, 103_000.0]
EQUITY_SHADOW = [100_000.0, 101_000.0, 100_500.0, 101_500.0, 102_000.0]


def nessuna_ombra(giorno: date, dati: Path) -> ShadowResult | None:
    return None


def cliente(
    sorgenti: Sorgenti, oggi: date = OGGI, base_url: str = LOOPBACK, ombra: CalcoloOmbra = nessuna_ombra
) -> TestClient:
    """Client sull'app costruita coi percorsi della fixture, un oggi fisso e uno shadow finto."""
    app = create_app(
        db=sorgenti.db,
        data_dir=sorgenti.data_dir,
        reports_dir=sorgenti.reports_dir,
        log=sorgenti.log,
        kill=sorgenti.kill,
        oggi=lambda: oggi,
        ombra=ombra,
    )
    return TestClient(app, base_url=base_url)


@pytest.fixture
def settimana(sorgenti: Sorgenti) -> Sorgenti:
    """Una settimana di live con un acquisto di SPY, e SPY e SHY nei Parquet."""
    sorgenti.data_dir.mkdir(parents=True)
    scrivi_serie(sorgenti.data_dir, "SPY", SETTIMANA, [100.0, 101.0, 102.0, 103.0, 104.0])
    scrivi_serie(sorgenti.data_dir, "SHY", SETTIMANA, [80.0, 80.004, 80.008, 80.012, 80.016])
    store = StateStore(sorgenti.db)
    for giorno, valore in zip(SETTIMANA, EQUITY_LIVE, strict=True):
        store.save_equity(giorno.date(), 0.0, valore)
    store.record_order("coid-1", OrderEvent(datetime(2026, 9, 7, 15, 45), "SPY", OrderDirection.BUY, 100))
    acquisto = FillEvent(datetime(2026, 9, 8, 9, 30), "SPY", OrderDirection.BUY, 100, 100.5, 1.0)
    store.record_fill("coid-1", acquisto)
    store.close()
    return sorgenti


def ombra_settimana(giorno: date, dati: Path) -> ShadowResult:
    """Shadow della stessa settimana con lo stesso acquisto, a slippage simulato."""
    return ShadowResult(
        start=SETTIMANA[0].date(),
        end=giorno,
        equity_curve=[(g.to_pydatetime(), v) for g, v in zip(SETTIMANA, EQUITY_SHADOW, strict=True)],
        fills=[
            FillEvent(datetime(2026, 9, 8), "SPY", OrderDirection.BUY, 100, 100.05, 1.0, slippage_cost=5.0)
        ],
    )


def riga(etichetta: str, *valori: str) -> str:
    """Riga della tabella delle metriche come la scrive il template."""
    return f"<tr><td>{etichetta}</td>" + "".join(f'<td class="num">{v}</td>' for v in valori) + "</tr>"


def test_home_mostra_posizioni_ed_equity(popolate: Sorgenti) -> None:
    risposta = cliente(popolate).get("/")

    assert risposta.status_code == 200
    assert risposta.headers["content-type"].startswith("text/html")
    pagina = risposta.text
    assert "10,300.00" in pagina and "9,800.00" in pagina
    assert "<td>SPY</td>" in pagina and "<td>IEF</td>" in pagina
    assert "run-b" in pagina and "coid-2" in pagina
    assert '<meta http-equiv="refresh" content="60">' in pagina
    assert 'href="/performance"' in pagina
    assert "<script" not in pagina
    assert "avviso allarme" not in pagina


def test_kill_switch_attivo_mostra_il_banner(popolate: Sorgenti) -> None:
    popolate.kill.write_text("eccezione in run_once: RuntimeError('broker')\n", encoding="utf-8")
    pagina = cliente(popolate).get("/").text

    assert '<div class="avviso allarme" role="alert">' in pagina
    assert "Kill switch attivo" in pagina
    assert "RuntimeError(&#39;broker&#39;)" in pagina
    assert "Dove guardare:" in pagina and "da riga di comando" in pagina


def test_database_assente_la_pagina_rende_comunque(sorgenti: Sorgenti) -> None:
    risposta = cliente(sorgenti).get("/")

    assert risposta.status_code == 200
    assert DB_ASSENTE in risposta.text
    assert "Nessuna esecuzione live registrata" in risposta.text
    assert "avviso allarme" not in risposta.text
    assert not sorgenti.db.exists()


def test_run_fallite_e_mismatch_recenti_nel_banner(popolate: Sorgenti) -> None:
    accoda_log(
        popolate.log,
        {"event": "RECONCILIATION_MISMATCH", "symbol": "TLT", "expected": 3, "actual": 1,
         "timestamp": "2026-08-01T13:35:00Z"},
        {"event": "RECONCILIATION_MISMATCH", "symbol": "SPY", "expected": 10, "actual": 8, "delta": -2,
         "timestamp": "2026-09-11T13:35:00Z"},
        {"event": "run_fallita", "run_id": "run-c", "errore": "ConnectionError: broker",
         "exception": "Traceback (most recent call last)", "timestamp": "2026-09-11T19:45:00Z"},
    )
    pagina = cliente(popolate).get("/").text

    assert "Esecuzioni fallite dal 2026-09-05: 1" in pagina
    assert "run-c" in pagina and "ConnectionError: broker" in pagina
    assert "RECONCILIATION_MISMATCH dal 2026-09-05: 1" in pagina
    assert "SPY attese 10, reali 8" in pagina
    assert "TLT" not in pagina, "un disallineamento di agosto non e' recente"
    assert "Traceback" not in pagina


def test_esecuzione_ferma_contata_sul_calendario_di_borsa(
    sorgenti: Sorgenti, calendar_parquet_dir: Path
) -> None:
    """Il venerdi' santo 2021 e' festivo: il lunedi' dopo sono passate due sedute, il martedi' tre."""
    passata = {"event": "run_conclusa", "run_id": "run-a", "timestamp": "2021-03-31T19:45:02Z"}
    scrivi_log(sorgenti.log, [passata])
    con_calendario = replace(sorgenti, data_dir=calendar_parquet_dir)

    lunedi = cliente(con_calendario, oggi=date(2021, 4, 5)).get("/").text
    assert "Nessuna esecuzione da" not in lunedi

    martedi = cliente(con_calendario, oggi=date(2021, 4, 6)).get("/").text
    assert "Nessuna esecuzione da 3 giornate di borsa" in martedi
    assert "2021-04-01, 2021-04-05, 2021-04-06" in martedi


def test_performance_grafici_inline_e_metriche_calcolate_a_mano(settimana: Sorgenti) -> None:
    parquet_prima = sorted(p.name for p in settimana.data_dir.iterdir())
    risposta = cliente(settimana, ombra=ombra_settimana).get("/performance")

    assert risposta.status_code == 200
    pagina = risposta.text
    assert pagina.count("<svg ") == 3
    assert "<script" not in pagina

    # Live: picco 102.000, minimo successivo 99.960. Shadow: picco 101.000, poi 100.500. SPY sale sempre.
    assert riga("Max drawdown", "-2.00%", "-0.50%", "0.00%") in pagina
    # Live: 100 azioni pagate 100.5 contro un'apertura di 100. Shadow: 5 di slippage simulato.
    assert riga("Slippage", "50", "5", "-") in pagina
    assert riga("Commissioni", "1", "1", "-") in pagina
    assert riga("Numero di trade", "1", "1", "-") in pagina
    assert "Sharpe (excess)" in pagina
    # Live +3,00% contro shadow +2,00% sulla stessa settimana.
    assert "+100.0 bps" in pagina

    assert not settimana.reports_dir.exists(), "la pagina non scrive report"
    assert sorted(p.name for p in settimana.data_dir.iterdir()) == parquet_prima


def test_shadow_rigiocato_una_volta_e_guasto_come_nota(settimana: Sorgenti) -> None:
    chiamate: list[date] = []

    def contata(giorno: date, dati: Path) -> ShadowResult:
        chiamate.append(giorno)
        return ombra_settimana(giorno, dati)

    client = cliente(settimana, ombra=contata)
    client.get("/performance")
    client.get("/performance")
    assert chiamate == [OGGI], "a dati e giornata invariati lo shadow non si rigioca"

    def guasta(giorno: date, dati: Path) -> ShadowResult:
        raise RuntimeError("universo incompleto")

    pagina = cliente(settimana, ombra=guasta).get("/performance").text
    assert "shadow non disponibile: RuntimeError: universo incompleto" in pagina
    assert pagina.count("<svg ") == 3, "live e SPY si disegnano anche senza shadow"


def test_performance_senza_dati_rende_comunque(sorgenti: Sorgenti) -> None:
    risposta = cliente(sorgenti).get("/performance")

    assert risposta.status_code == 200
    assert "<svg" not in risposta.text
    assert "Nessun periodo da confrontare" in risposta.text


def test_filtri_degli_ordini_restringono_le_righe(popolate: Sorgenti) -> None:
    store = StateStore(popolate.db)
    vecchio = OrderEvent(datetime(2026, 9, 1, 15, 45), "SPY", OrderDirection.SELL, 5)
    store.record_order("coid-vecchio", vecchio, status="canceled")
    store.close()
    client = cliente(popolate)

    tutti = client.get("/ordini").text
    assert all(codice in tutti for codice in ("coid-0", "coid-1", "coid-2", "coid-vecchio"))
    assert '<span class="stato stato-chiuso">canceled</span>' in tutti

    solo_spy = client.get("/ordini", params={"simbolo": "spy"}).text
    assert "coid-0" in solo_spy and "coid-vecchio" in solo_spy
    assert "coid-1" not in solo_spy and "coid-2" not in solo_spy

    un_giorno = client.get("/ordini", params={"dal": "2026-09-10", "al": "2026-09-10"}).text
    assert "coid-0" in un_giorno and "coid-vecchio" not in un_giorno
    assert "Nessun eseguito dal 2026-09-10 al 2026-09-10." in un_giorno

    storto = client.get("/ordini", params={"dal": "ieri", "simbolo": "SPY;DROP"})
    assert storto.status_code == 200
    assert "non valida" in storto.text and "non valido" in storto.text


def test_eseguiti_confrontati_con_lo_shadow(popolate: Sorgenti) -> None:
    def ombra(giorno: date, dati: Path) -> ShadowResult:
        return ShadowResult(
            start=date(2026, 9, 10),
            end=giorno,
            fills=[
                FillEvent(datetime(2026, 9, 11), "SPY", OrderDirection.BUY, 10, 99.5, 1.0),
                FillEvent(datetime(2026, 9, 11), "TLT", OrderDirection.BUY, 7, 90.0, 1.0),
            ],
        )

    pagina = cliente(popolate, ombra=ombra).get("/ordini").text
    # SPY pagato 100,00 contro 99,50 teorici: (100 / 99,5 - 1) * 10.000 = +50,3 bps.
    assert '<td class="num">+50.3</td>' in pagina
    assert "Eseguiti teorici senza corrispettivo live" in pagina and "<td>TLT</td>" in pagina


def test_rischio_per_motivo_con_le_anomalie_in_evidenza(popolate: Sorgenti) -> None:
    accoda_log(
        popolate.log,
        {"event": "posizione_senza_barre", "symbol": "IEF", "quantity": 4, "giorni_di_ritardo": 5,
         "timestamp": "2026-09-11T19:45:00Z"},
        {"event": "kill_switch_attivato", "reason": "eccezione in run_once",
         "timestamp": "2026-09-11T19:45:05Z"},
    )
    client = cliente(popolate)

    settimana = client.get("/rischio", params={"giorni": "7"}).text
    assert "<code>MAX_NOTIONAL_PER_ORDER</code>" in settimana and "<code>KILL_SWITCH</code>" in settimana
    assert "MAX_DRAWDOWN" not in settimana, "la decisione del primo settembre e' fuori finestra"
    assert '<span class="esito esito-ridotta">ridotta</span>' in settimana
    assert '<span class="esito esito-rifiutata">rifiutata</span>' in settimana
    assert settimana.count('<tr class="evidenza">') == 2
    assert "posizione_senza_barre" in settimana and "kill_switch_attivato" in settimana

    vuota = client.get("/rischio", params={"giorni": "1"}).text
    assert "Nessuna decisione del gestore del rischio dal 2026-09-11 al 2026-09-11." in vuota

    storta = client.get("/rischio", params={"giorni": "abc"})
    assert storta.status_code == 200 and "Dal 2026-09-05 al 2026-09-11" in storta.text


PNG = b"\x89PNG\r\n\x1a\n" + bytes(16)


@pytest.fixture
def con_report(sorgenti: Sorgenti) -> Sorgenti:
    """Report settimanale con il suo grafico, e un segreto fuori dalla cartella raggiunto da un link."""
    cartella = sorgenti.reports_dir
    cartella.mkdir(parents=True)
    markdown = "# Settimana 2026-37\n\n![equity](weekly_2026-37.png)\n<b>non html</b>\n"
    (cartella / "weekly_2026-37.md").write_text(markdown, encoding="utf-8")
    (cartella / "weekly_2026-37.png").write_bytes(PNG)
    segreto = cartella.parent / "segreto.txt"
    segreto.write_text("password-del-broker", encoding="utf-8")
    (cartella / "link.txt").symlink_to(segreto)
    return sorgenti


def test_report_elenco_testo_e_immagini(con_report: Sorgenti) -> None:
    client = cliente(con_report)

    elenco = client.get("/report").text
    assert 'href="/report/weekly_2026-37.md"' in elenco and 'href="/report/weekly_2026-37.png"' in elenco
    assert "link.txt" not in elenco

    testo = client.get("/report/weekly_2026-37.md")
    assert testo.status_code == 200
    assert '<pre class="testo-report"># Settimana 2026-37' in testo.text
    assert "&lt;b&gt;non html&lt;/b&gt;" in testo.text
    assert '<img src="/report/weekly_2026-37.png"' in testo.text

    immagine = client.get("/report/weekly_2026-37.png")
    assert immagine.status_code == 200
    assert immagine.headers["content-type"] == "image/png"
    assert immagine.content == PNG


@pytest.mark.parametrize(
    "percorso",
    [
        "/report/..%2Fsegreto.txt",
        "/report/%2e%2e/segreto.txt",
        "/report/sotto/..%2F..%2Fsegreto.txt",
        "/report/%2Fetc%2Fpasswd",
        "/report//etc/passwd",
        "/report/link.txt",
        "/report/manca.md",
    ],
)
def test_report_fuori_dalla_cartella_danno_404(con_report: Sorgenti, percorso: str) -> None:
    risposta = cliente(con_report).get(percorso)
    assert risposta.status_code == 404
    assert "password-del-broker" not in risposta.text


def test_report_senza_cartella(sorgenti: Sorgenti) -> None:
    risposta = cliente(sorgenti).get("/report")
    assert risposta.status_code == 200 and "assente" in risposta.text


def test_healthz_non_apre_il_database(popolate: Sorgenti, monkeypatch: pytest.MonkeyPatch) -> None:
    def vietato(*_: object, **__: object) -> StateStore:
        raise AssertionError("il database e' stato aperto")

    monkeypatch.setattr(StateStore, "read_only", vietato)
    client = cliente(popolate)

    risposta = client.get("/healthz")
    assert risposta.status_code == 200
    assert risposta.json() == {"stato": "ok"}
    with pytest.raises(AssertionError, match="aperto"):
        client.get("/")


def test_solo_get_solo_loopback_niente_cookie(popolate: Sorgenti) -> None:
    client = cliente(popolate)
    for rotta in client.app.routes:  # type: ignore[attr-defined]
        if isinstance(rotta, Mount):
            continue
        assert set(rotta.methods) <= {"GET", "HEAD"}, rotta.path

    assert client.post("/").status_code == 405
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/static/stile.css").status_code == 200

    risposta = client.get("/")
    assert "set-cookie" not in risposta.headers
    assert "default-src 'none'" in risposta.headers["content-security-policy"]

    esterno = cliente(popolate, base_url="http://dashboard.attaccante.example")
    assert esterno.get("/").status_code == 400
