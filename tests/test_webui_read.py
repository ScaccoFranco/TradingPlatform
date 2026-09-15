"""Strato dati della dashboard: valori attesi, sola lettura, sorgenti assenti."""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sintetici import accoda_log, scrivi_log

from quant.webui.read import (
    DB_ASSENTE,
    Posizione,
    Sorgenti,
    anomalie_recenti,
    apri_sola_lettura,
    decisioni_rischio,
    elenco_report,
    fill_recenti,
    ordini_recenti,
    rischio,
    risolvi_report,
    ritardo_esecuzione,
    serie_equity,
    stato_live,
)

OGGI = date(2026, 9, 11)


def test_stato_live_legge_posizioni_equity_ed_esecuzione(popolate: Sorgenti) -> None:
    stato = stato_live(popolate)

    assert stato.disponibile and stato.nota == ""
    assert stato.posizioni == (Posizione("IEF", 4), Posizione("SPY", 10))
    assert stato.giorno_equity == date(2026, 9, 10)
    assert (stato.cassa, stato.valore_posizioni, stato.equity) == (500.0, 9_800.0, 10_300.0)
    assert not stato.kill_switch.attivo and stato.kill_switch.motivo == ""
    esecuzione = stato.ultima_esecuzione
    assert esecuzione is not None and stato.nota_log == ""
    assert esecuzione.run_id == "run-b" and not esecuzione.fallita
    assert esecuzione.momento == datetime(2026, 9, 10, 19, 45, 2, tzinfo=UTC)


def test_run_fallita_e_kill_switch_con_motivo(popolate: Sorgenti) -> None:
    accoda_log(popolate.log, {"event": "run_fallita", "run_id": "run-c", "timestamp": "2026-09-11T19:45:00Z"})
    popolate.kill.write_text("eccezione in run_once: RuntimeError('broker')\n", encoding="utf-8")

    stato = stato_live(popolate)
    assert stato.kill_switch.attivo
    assert stato.kill_switch.motivo == "eccezione in run_once: RuntimeError('broker')"
    assert stato.ultima_esecuzione is not None
    assert stato.ultima_esecuzione.fallita and stato.ultima_esecuzione.run_id == "run-c"


def test_equity_ordini_ed_eseguiti(popolate: Sorgenti) -> None:
    equity = serie_equity(popolate, giorni=30)
    assert equity.disponibile
    assert [p.giorno for p in equity.righe] == [date(2026, 9, 9), date(2026, 9, 10)]
    assert equity.righe[-1].totale == 10_300.0
    assert len(serie_equity(popolate, giorni=1).righe) == 1

    assert [o.client_order_id for o in ordini_recenti(popolate, 2).righe] == ["coid-2", "coid-1"]
    eseguiti = fill_recenti(popolate, 1).righe
    assert [(f.client_order_id, f.fill_price) for f in eseguiti] == [("coid-2", 102.0)]


def test_decisioni_del_rischio_nella_finestra(popolate: Sorgenti) -> None:
    settimana = decisioni_rischio(popolate, giorni=7, oggi=OGGI)
    assert settimana.disponibile
    assert (settimana.dal, settimana.al) == (date(2026, 9, 5), OGGI)
    assert [(d.symbol, d.motivo) for d in settimana.righe] == [
        ("QQQ", "MAX_NOTIONAL_PER_ORDER"),
        ("GLD", "KILL_SWITCH"),
    ]
    ridotta, rifiutata = settimana.righe
    assert ridotta.ridotta and not ridotta.rifiutata
    assert (ridotta.quantita_richiesta, ridotta.quantita_finale, ridotta.run_id) == (100, 50, "run-b")
    assert rifiutata.rifiutata and not rifiutata.ridotta

    assert len(decisioni_rischio(popolate, giorni=15, oggi=OGGI).righe) == 3
    vuota = decisioni_rischio(popolate, giorni=1, oggi=OGGI)
    assert vuota.disponibile and vuota.righe == ()


def test_anomalie_nella_finestra_senza_stack_trace(popolate: Sorgenti) -> None:
    accoda_log(
        popolate.log,
        {"event": "RECONCILIATION_MISMATCH", "symbol": "TLT", "expected": 3, "actual": 1,
         "timestamp": "2026-08-01T13:35:00Z"},
        {"event": "posizione_senza_barre", "symbol": "IEF", "quantity": 4, "ultima_barra": "2026-09-08",
         "giorni_di_ritardo": 3, "timestamp": "2026-09-11T19:45:00Z"},
        {"event": "run_fallita", "run_id": "run-c", "errore": "boom", "exception": "Traceback ...",
         "timestamp": "2026-09-11T19:45:01Z"},
    )
    anomalie = anomalie_recenti(popolate, giorni=7, oggi=OGGI)
    assert [a.evento for a in anomalie.righe] == ["posizione_senza_barre", "run_fallita"]
    ferma, fallita = anomalie.righe
    assert ferma.symbol == "IEF" and ferma.campo("giorni_di_ritardo") == "3"
    assert fallita.run_id == "run-c" and fallita.campo("errore") == "boom"
    assert fallita.campo("exception") is None and "Traceback" not in fallita.dettaglio


def test_ritardo_del_runner_dalla_data_piu_recente(popolate: Sorgenti) -> None:
    """Senza Parquet si contano i feriali: dal 10 al 15 settembre sono tre sedute."""
    ritardo = ritardo_esecuzione(popolate, stato_live(popolate), oggi=date(2026, 9, 15))
    assert ritardo is not None and ritardo.nota == ""
    assert (ritardo.ultima, ritardo.fonte) == (date(2026, 9, 10), "log")
    assert ritardo.sedute == (date(2026, 9, 11), date(2026, 9, 14), date(2026, 9, 15))


def test_scrittura_sulla_connessione_in_sola_lettura_solleva(popolate: Sorgenti) -> None:
    prima = hashlib.sha256(popolate.db.read_bytes()).hexdigest()
    with apri_sola_lettura(popolate.db) as store:
        assert store is not None
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            store.save_positions({"SPY": 1})
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            store.connection.execute("CREATE TABLE intrusa (x)")

    stato_live(popolate)
    serie_equity(popolate)
    ordini_recenti(popolate)
    fill_recenti(popolate)
    assert hashlib.sha256(popolate.db.read_bytes()).hexdigest() == prima


def test_sorgenti_assenti_danno_uno_stato_vuoto_ma_valido(sorgenti: Sorgenti) -> None:
    stato = stato_live(sorgenti)
    assert not stato.disponibile and stato.nota == DB_ASSENTE
    assert stato.posizioni == () and stato.equity is None and stato.giorno_equity is None
    assert stato.ultima_esecuzione is None and "assente" in stato.nota_log
    assert not stato.kill_switch.attivo
    assert ritardo_esecuzione(sorgenti, stato, OGGI) is None

    for elenco in (serie_equity(sorgenti), ordini_recenti(sorgenti), fill_recenti(sorgenti)):
        assert not elenco.disponibile and elenco.nota == DB_ASSENTE and elenco.righe == ()
    for finestra in (decisioni_rischio(sorgenti, oggi=OGGI), anomalie_recenti(sorgenti, oggi=OGGI)):
        assert not finestra.disponibile and "assente" in finestra.nota and finestra.righe == ()

    assert not sorgenti.db.parent.exists(), "la lettura non deve creare il database"
    assert not sorgenti.log.parent.exists()


def test_database_illeggibile_e_log_senza_passate_non_sollevano(sorgenti: Sorgenti) -> None:
    sorgenti.db.parent.mkdir(parents=True)
    sorgenti.db.touch()
    scrivi_log(sorgenti.log, [{"event": "stato_caricato", "timestamp": "2026-09-10T19:45:00Z"}])

    stato = stato_live(sorgenti)
    assert not stato.disponibile and "non leggibile" in stato.nota
    assert stato.ultima_esecuzione is None and "nessuna esecuzione" in stato.nota_log
    assert not ordini_recenti(sorgenti).disponibile


def test_decisioni_raggruppate_per_motivo(popolate: Sorgenti) -> None:
    accoda_log(
        popolate.log,
        {"event": "ordine_ridotto", "symbol": "SPY", "quantity_original": 50, "quantity_final": 20,
         "reason": "MAX_WEIGHT_PER_SYMBOL+MAX_NOTIONAL_PER_ORDER", "timestamp": "2026-09-11T19:45:00Z"},
    )
    dati = rischio(popolate, giorni=7, oggi=OGGI)

    per_motivo = {gruppo.motivo: gruppo for gruppo in dati.gruppi}
    assert set(per_motivo) == {"MAX_NOTIONAL_PER_ORDER", "KILL_SWITCH", "MAX_WEIGHT_PER_SYMBOL"}
    assert dati.gruppi[0].motivo == "MAX_NOTIONAL_PER_ORDER", "il gruppo piu' numeroso viene prima"
    assert [d.symbol for d in per_motivo["MAX_NOTIONAL_PER_ORDER"].ridotte] == ["QQQ", "SPY"]
    assert [d.symbol for d in per_motivo["KILL_SWITCH"].rifiutate] == ["GLD"]
    assert (len(dati.ridotte), len(dati.rifiutate), len(dati.decisioni)) == (2, 1, 3)


def test_i_report_si_risolvono_solo_dentro_la_cartella(sorgenti: Sorgenti, tmp_path: Path) -> None:
    cartella = sorgenti.reports_dir
    (cartella / "sotto").mkdir(parents=True)
    (cartella / "weekly_2026-37.md").write_text("# ok\n", encoding="utf-8")
    (cartella / "sotto" / "reconcile.csv").write_text("day\n", encoding="utf-8")
    (cartella / ".nascosto").write_text("x", encoding="utf-8")
    segreto = tmp_path / "segreto.txt"
    segreto.write_text("password", encoding="utf-8")
    (cartella / "link.txt").symlink_to(segreto)

    assert risolvi_report(cartella, "weekly_2026-37.md") == (cartella / "weekly_2026-37.md").resolve()
    assert risolvi_report(cartella, "sotto/reconcile.csv") is not None
    fuori = [
        "../segreto.txt",
        "sotto/../../segreto.txt",
        str(segreto),
        "/etc/passwd",
        ".nascosto",
        "link.txt",
        "sotto",
        "",
        "manca.md",
        "a\x00b",
    ]
    for nome in fuori:
        assert risolvi_report(cartella, nome) is None, nome

    assert {f.nome for f in elenco_report(sorgenti).righe} == {"weekly_2026-37.md", "sotto/reconcile.csv"}
    assert not elenco_report(replace(sorgenti, reports_dir=tmp_path / "manca")).disponibile


def test_lo_strato_dati_non_importa_broker_ne_rete() -> None:
    codice = (
        "import sys, quant.webui.app\n"
        "vietati = ('alpaca', 'yfinance', 'requests', 'quant.brokers', 'quant.live')\n"
        "print(sorted(m for m in sys.modules if m.startswith(vietati)))"
    )
    esito = subprocess.run([sys.executable, "-c", codice], capture_output=True, text=True, check=True)
    assert esito.stdout.strip() == "[]"
