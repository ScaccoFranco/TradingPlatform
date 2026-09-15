"""Test del report settimanale su StateStore sintetico."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from sintetici import scrivi_log

from quant.events import FillEvent, OrderDirection, OrderEvent
from quant.live.schedule import ORA_REPORT_SETTIMANALE, build_scheduler, prossime_esecuzioni, weekly_trigger
from quant.logreader import decisions_by_reason, read_events, risk_decisions, run_days, split_reasons
from quant.shadow import ShadowResult
from quant.state import StateStore
from quant.weekly import build_weekly_report, confronto_live, giorni_di_borsa_dopo, week_bounds

LUNEDI = date(2026, 9, 7)
VENERDI = date(2026, 9, 11)
CAPITALE = 100_000.0


def settimana_di_borsa() -> list[date]:
    """Le cinque giornate della settimana di riferimento."""
    return [LUNEDI + pd.Timedelta(days=i) for i in range(5)]


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "live.db")


@pytest.fixture
def dati(tmp_path: Path) -> Path:
    """Parquet minimo di SPY: serve come calendario e come benchmark."""
    directory = tmp_path / "parquet"
    directory.mkdir()
    giorni = pd.DatetimeIndex([pd.Timestamp(g) for g in settimana_di_borsa()])
    closes = [100.0, 101.0, 102.0, 103.0, 104.0]
    frame = pd.DataFrame(
        {
            "open": [100.0] * 5,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "adj_close": closes,
            "volume": [1_000_000.0] * 5,
        },
        index=giorni,
    )
    frame.index.name = "date"
    frame.to_parquet(directory / "SPY.parquet")
    return directory


def popola_store(store: StateStore, equity: list[float], prezzo_fill: float = 100.5) -> None:
    """Una settimana di equity live e un eseguito reale il martedi'."""
    for giorno, valore in zip(settimana_di_borsa(), equity, strict=False):
        store.save_equity(giorno, 0.0, valore)
    ordine = OrderEvent(datetime(2026, 9, 7, 15, 45), "SPY", OrderDirection.BUY, 100)
    store.record_order("coid-1", ordine)
    eseguito = FillEvent(datetime(2026, 9, 8), "SPY", OrderDirection.BUY, 100, prezzo_fill, 1.0)
    store.record_fill("coid-1", eseguito)


def ombra(equity: list[float], prezzo_fill: float = 100.05) -> ShadowResult:
    """Shadow con la stessa operazione e l'equity teorica indicata."""
    return ShadowResult(
        start=LUNEDI,
        end=VENERDI,
        equity_curve=[
            (datetime.combine(g, datetime.min.time()), v)
            for g, v in zip(settimana_di_borsa(), equity, strict=False)
        ],
        fills=[FillEvent(datetime(2026, 9, 8), "SPY", OrderDirection.BUY, 100, prezzo_fill, 1.0)],
    )


def test_confini_della_settimana() -> None:
    assert week_bounds(date(2026, 9, 9)) == (LUNEDI, date(2026, 9, 13))
    assert week_bounds(LUNEDI)[0] == LUNEDI


def test_report_completo_senza_allarmi(store: StateStore, dati: Path, tmp_path: Path) -> None:
    """Live e shadow allineati: nessun blocco di attenzione."""
    equity = [CAPITALE * (1 + 0.001 * i) for i in range(5)]
    popola_store(store, equity, prezzo_fill=100.05)
    report = build_weekly_report(
        store,
        ombra(equity),
        riferimento=VENERDI,
        directory=tmp_path / "reports",
        data_path=dati,
        log_path=tmp_path / "assente.jsonl",
    )

    assert report.path.name == "weekly_2026-37.md"
    assert report.path.exists()
    assert report.chart is not None and report.chart.exists()
    assert report.attenzione is False
    assert "ATTENZIONE" not in report.markdown
    assert "| Giorno | Simbolo | Stato |" in report.markdown
    assert len(report.summary_lines) == 3
    assert str(report.path) in report.alert_text


def test_soglia_di_tracking_error_fa_scattare_lattenzione(
    store: StateStore, dati: Path, tmp_path: Path
) -> None:
    """Il live perde un punto percentuale sullo shadow: ben oltre i 50 bps."""
    live = [CAPITALE, CAPITALE, CAPITALE, CAPITALE, CAPITALE * 0.99]
    teorica = [CAPITALE] * 5
    popola_store(store, live, prezzo_fill=100.05)
    report = build_weekly_report(
        store,
        ombra(teorica),
        riferimento=VENERDI,
        directory=tmp_path / "reports",
        data_path=dati,
        log_path=tmp_path / "assente.jsonl",
    )

    assert report.attenzione is True
    assert report.markdown.startswith("> **ATTENZIONE**")
    assert "Tracking error cumulato" in report.markdown


def test_slippage_doppio_fa_scattare_lattenzione(store: StateStore, dati: Path, tmp_path: Path) -> None:
    equity = [CAPITALE] * 5
    popola_store(store, equity, prezzo_fill=100.5)
    report = build_weekly_report(
        store,
        ombra(equity, prezzo_fill=100.05),
        riferimento=VENERDI,
        directory=tmp_path / "reports",
        data_path=dati,
        log_path=tmp_path / "assente.jsonl",
    )

    assert report.attenzione is True
    assert "volte tanto" in report.markdown


def test_soglia_configurabile(store: StateStore, dati: Path, tmp_path: Path) -> None:
    live = [CAPITALE, CAPITALE, CAPITALE, CAPITALE, CAPITALE * 0.99]
    popola_store(store, live, prezzo_fill=100.05)
    permissivo = build_weekly_report(
        store,
        ombra([CAPITALE] * 5),
        riferimento=VENERDI,
        directory=tmp_path / "reports",
        data_path=dati,
        log_path=tmp_path / "assente.jsonl",
        soglia_tracking_error_bps=5_000.0,
    )
    assert permissivo.attenzione is False


def test_giorni_saltati_e_anomalie_dai_log(store: StateStore, dati: Path, tmp_path: Path) -> None:
    """Equity solo per tre giorni su cinque e un mismatch nel log."""
    for giorno, valore in zip(settimana_di_borsa()[:3], [CAPITALE] * 3, strict=False):
        store.save_equity(giorno, 0.0, valore)
    log = scrivi_log(
        tmp_path / "logs" / "live.jsonl",
        [
            {"event": "RECONCILIATION_MISMATCH", "symbol": "SPY", "expected": 10, "actual": 8,
             "timestamp": "2026-09-09T13:35:00Z", "level": "error"},
            {"event": "ordine_ridotto", "symbol": "QQQ", "quantity_original": 100, "quantity_final": 50,
             "reason": "MAX_NOTIONAL_PER_ORDER", "timestamp": "2026-09-09T19:45:00Z", "level": "warning"},
            {"event": "run_conclusa", "timestamp": "2026-09-09T19:45:10Z", "level": "info"},
        ],
    )

    report = build_weekly_report(
        store,
        ombra([CAPITALE] * 5),
        riferimento=VENERDI,
        directory=tmp_path / "reports",
        data_path=dati,
        log_path=log,
    )

    assert report.attenzione is True
    assert "RECONCILIATION_MISMATCH: 1" in report.markdown
    assert "MAX_NOTIONAL_PER_ORDER" in report.markdown
    assert "Giornate di borsa senza esecuzione: 2" in report.markdown


def test_lettura_dei_log_filtra_per_periodo(tmp_path: Path) -> None:
    log = scrivi_log(
        tmp_path / "live.jsonl",
        [
            {"event": "ordine_rifiutato", "timestamp": "2026-09-01T19:45:00Z"},
            {"event": "ordine_approvato", "timestamp": "2026-09-09T19:45:00Z"},
            {"event": "run_conclusa", "timestamp": "2026-09-09T19:46:00Z"},
            {"riga": "non un evento"},
        ],
    )
    eventi = read_events(log, since=LUNEDI, until=VENERDI)
    assert len(eventi) == 2
    assert len(risk_decisions(eventi)) == 1
    assert run_days(eventi) == {date(2026, 9, 9)}
    assert read_events(tmp_path / "manca.jsonl") == []


def test_il_job_settimanale_e_registrato_di_lunedi() -> None:
    calendario = SimpleNamespace(get_calendar=lambda request: [])
    scheduler = build_scheduler(
        calendario, lambda: None, lambda: None, lambda: None, scheduler=BackgroundScheduler()
    )
    assert {job.id for job in scheduler.get_jobs()} == {"trading", "riconciliazione", "report_settimanale"}

    scatti = prossime_esecuzioni(weekly_trigger(*ORA_REPORT_SETTIMANALE), 4)
    assert all(s.weekday() == 0 for s in scatti)
    assert {(s.hour, s.minute) for s in scatti} == {ORA_REPORT_SETTIMANALE}


def test_il_live_scrive_json_che_il_report_sa_rileggere(tmp_path: Path) -> None:
    """Il formato del file di log e' il contratto fra scheduler e report settimanale."""
    from quant.logging import configure_for_live, get_logger, reset_for_tests

    percorso = tmp_path / "logs" / "live.jsonl"
    try:
        assert configure_for_live(percorso) == percorso
        get_logger("risk").warning(
            "ordine_rifiutato", symbol="SPY", quantity_original=10, quantity_final=0, reason="KILL_SWITCH"
        )
        get_logger("state").error("RECONCILIATION_MISMATCH", symbol="IEF", expected=5, actual=4)
    finally:
        reset_for_tests()

    eventi = read_events(percorso)
    assert [e["event"] for e in eventi] == ["ordine_rifiutato", "RECONCILIATION_MISMATCH"]
    assert risk_decisions(eventi)[0]["reason"] == "KILL_SWITCH"



def test_provenienza_dello_shadow_in_coda(store: StateStore, dati: Path, tmp_path: Path) -> None:
    """Il report finisce con la provenienza dello shadow, o dice che non c'e'."""
    from quant.config import BacktestConfig
    from quant.provenance import build_provenance
    from quant.strategy import BuyAndHoldStrategy

    equity = [CAPITALE * (1 + 0.001 * i) for i in range(5)]
    popola_store(store, equity, prezzo_fill=100.05)
    opzioni = {"riferimento": VENERDI, "data_path": dati, "log_path": tmp_path / "assente.jsonl"}
    senza = build_weekly_report(store, ombra(equity), directory=tmp_path / "a", **opzioni)
    assert "## Provenienza dello shadow\n\nNon disponibile" in senza.markdown

    shadow = ombra(equity)
    strategia = BuyAndHoldStrategy(None, "SPY")
    shadow.provenance = build_provenance(BacktestConfig(path=dati), ["SPY"], None, None, None, strategia)
    con = build_weekly_report(store, shadow, directory=tmp_path / "b", **opzioni)
    coda = con.markdown[con.markdown.index("## Provenienza dello shadow") :]
    assert shadow.provenance.hash in coda
    assert con.markdown.rstrip().splitlines()[-1].startswith("- Config:")


def test_giorni_di_borsa_dopo_calendario_poi_feriali(dati: Path, calendar_parquet_dir: Path) -> None:
    """Dentro i Parquet valgono le festivita', oltre l'ultima barra si contano i feriali."""
    assert giorni_di_borsa_dopo(date(2021, 3, 31), date(2021, 4, 6), calendar_parquet_dir) == [
        date(2021, 4, 1),
        date(2021, 4, 5),
        date(2021, 4, 6),
    ]
    assert giorni_di_borsa_dopo(date(2026, 9, 9), date(2026, 9, 15), dati) == [
        date(2026, 9, 10),
        date(2026, 9, 11),
        date(2026, 9, 14),
        date(2026, 9, 15),
    ]
    assert giorni_di_borsa_dopo(VENERDI, VENERDI, dati) == []
    assert giorni_di_borsa_dopo(VENERDI, date(2026, 9, 14), dati / "vuota") == [date(2026, 9, 14)]


def test_confronto_live_misura_i_tre_lati_allo_stesso_modo(store: StateStore, dati: Path) -> None:
    equity = [CAPITALE * (1 + 0.001 * i) for i in range(5)]
    popola_store(store, equity, prezzo_fill=100.5)
    confronto = confronto_live(store, ombra([CAPITALE] * 5, prezzo_fill=100.05), dati)

    assert (confronto.inizio, confronto.fine) == (LUNEDI, VENERDI)
    assert list(confronto.benchmark) == [100.0, 101.0, 102.0, 103.0, 104.0]
    assert list(confronto.esposizione) == [1.0] * 5
    live, shadow = confronto.metriche["live"], confronto.metriche["shadow"]
    assert live["rendimento_totale"] == pytest.approx(0.004)
    assert live["slippage"] == pytest.approx(50.0), "100 azioni pagate 0.5 sopra l'apertura di 100"
    assert (live["n_trade"], live["commissioni"]) == (1.0, 1.0)
    assert shadow["rendimento_totale"] == 0.0 and shadow["n_trade"] == 1.0
    assert confronto.metriche["SPY"]["max_drawdown"] == 0.0
    assert confronto.tracking_error["cumulative_bps"] == pytest.approx(40.0)
    assert confronto.drawdown["live"].min() == 0.0

    vuoto = confronto_live(None, None, dati)
    assert vuoto.inizio is None and vuoto.benchmark.empty and vuoto.live.empty


def test_motivi_del_rischio_separati_e_raggruppati() -> None:
    """Il RiskManager unisce i motivi con '+': ognuno conta nel proprio gruppo."""
    assert split_reasons("MAX_WEIGHT_PER_SYMBOL+MAX_NOTIONAL_PER_ORDER") == (
        "MAX_WEIGHT_PER_SYMBOL",
        "MAX_NOTIONAL_PER_ORDER",
    )
    assert split_reasons("") == ("SENZA_MOTIVO",)
    eventi = [
        {"event": "ordine_ridotto", "reason": "A+B"},
        {"event": "ordine_rifiutato", "reason": "B"},
        {"event": "run_conclusa"},
    ]
    gruppi = decisions_by_reason(eventi)
    assert {motivo: len(decisioni) for motivo, decisioni in gruppi.items()} == {"A": 1, "B": 2}
