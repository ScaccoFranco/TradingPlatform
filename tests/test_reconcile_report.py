"""Test del confronto fra eseguiti reali e teorici."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from quant.events import FillEvent, OrderDirection, OrderEvent
from quant.reconcile_report import (
    APPAIATO,
    SOLO_LIVE,
    SOLO_SHADOW,
    FillRecord,
    compare,
    live_equity_series,
    live_exposure_series,
    real_fill_events,
    real_fills,
    reference_opens,
    summarize,
    theoretical_fills,
    tracking_error,
    write_report,
)
from quant.shadow import ShadowResult
from quant.state import StateStore

GIORNO = datetime(2026, 9, 8, 9, 30)
APERTURA = 100.0


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "live.db")


def popola(store: StateStore, prezzo: float = 100.5, symbol: str = "SPY") -> None:
    """Un ordine deciso il giorno prima e il suo eseguito all'apertura successiva."""
    ordine = OrderEvent(datetime(2026, 9, 7, 15, 45), symbol, OrderDirection.BUY, 100)
    store.record_order("coid-1", ordine)
    store.record_fill("coid-1", FillEvent(GIORNO, symbol, OrderDirection.BUY, 100, prezzo, 1.0))


def ombra(prezzo: float = 100.05, symbol: str = "SPY", quantita: int = 100) -> ShadowResult:
    """Shadow con una sola operazione e due giorni di equity."""
    return ShadowResult(
        start=date(2026, 9, 7),
        end=date(2026, 9, 8),
        equity_curve=[(datetime(2026, 9, 7), 100_000.0), (datetime(2026, 9, 8), 100_500.0)],
        fills=[FillEvent(GIORNO, symbol, OrderDirection.BUY, quantita, prezzo, 1.0)],
    )


def aperture(symbol: str = "SPY") -> dict[str, pd.Series]:
    """Apertura ufficiale di riferimento per la giornata dell'eseguito."""
    return {symbol: pd.Series([APERTURA], index=pd.DatetimeIndex([pd.Timestamp("2026-09-08")]))}


def test_slippage_noto_viene_misurato(store: StateStore) -> None:
    """Reale a 50 bps sopra l'apertura, teorico a 5: il report deve dire esattamente questo."""
    popola(store, prezzo=100.5)
    confronti = compare(real_fills(store), theoretical_fills(ombra(100.05)), aperture())

    assert len(confronti) == 1
    confronto = confronti[0]
    assert confronto.status == APPAIATO
    assert confronto.slippage_real_bps == pytest.approx(50.0)
    assert confronto.slippage_theoretical_bps == pytest.approx(5.0)
    assert confronto.diff_bps == pytest.approx(44.98, abs=0.01)
    assert confronto.quantity_diff == 0


def test_slippage_di_una_vendita_ha_segno_coerente(store: StateStore) -> None:
    """Vendere sotto l'apertura e' sfavorevole tanto quanto comprare sopra."""
    ordine = OrderEvent(datetime(2026, 9, 7, 15, 45), "SPY", OrderDirection.SELL, 100)
    store.record_order("coid-2", ordine)
    store.record_fill("coid-2", FillEvent(GIORNO, "SPY", OrderDirection.SELL, 100, 99.5, 1.0))

    confronti = compare(real_fills(store), [], aperture())
    assert confronti[0].slippage_real_bps == pytest.approx(50.0)


def test_fill_mancante_lato_broker(store: StateStore) -> None:
    confronti = compare(real_fills(store), theoretical_fills(ombra()), aperture())
    assert [c.status for c in confronti] == [SOLO_SHADOW]
    assert confronti[0].real is None
    assert confronti[0].diff_bps is None

    sommario = summarize(confronti)
    assert (sommario.n_matched, sommario.n_solo_live, sommario.n_solo_shadow) == (0, 0, 1)


def test_fill_presente_solo_in_live(store: StateStore) -> None:
    popola(store)
    confronti = compare(real_fills(store), [], aperture())
    assert [c.status for c in confronti] == [SOLO_LIVE]
    assert summarize(confronti).n_solo_live == 1


def test_ritardo_fra_segnale_ed_eseguito(store: StateStore) -> None:
    popola(store)
    reale = real_fills(store)[0]
    teorico = theoretical_fills(ombra())[0]

    assert reale.delay_days == pytest.approx((GIORNO - datetime(2026, 9, 7, 15, 45)).total_seconds() / 86400)
    assert teorico.delay_days == pytest.approx(1.0)


def test_quantita_diversa_viene_riportata(store: StateStore) -> None:
    popola(store)
    confronti = compare(real_fills(store), theoretical_fills(ombra(quantita=80)), aperture())
    assert confronti[0].quantity_diff == 20


def test_tracking_error_su_curve_note() -> None:
    date_range = pd.bdate_range("2026-09-01", periods=5)
    live = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0], index=date_range)
    shadow = pd.Series([100.0, 101.0, 102.0, 103.0, 105.0], index=date_range)

    errore = tracking_error(live, shadow)
    assert errore["days"] == 5
    assert errore["cumulative_bps"] == pytest.approx(-100.0, abs=0.01)
    assert errore["daily_bps"] > 0

    identiche = tracking_error(live, live.copy())
    assert identiche["cumulative_bps"] == pytest.approx(0.0)
    assert identiche["daily_bps"] == pytest.approx(0.0, abs=1e-9)


def test_sintesi_e_scrittura_dei_file(store: StateStore, tmp_path: Path) -> None:
    popola(store, prezzo=100.5)
    store.save_equity(date(2026, 9, 7), 0.0, 100_000.0)
    store.save_equity(date(2026, 9, 8), 0.0, 100_400.0)

    risultato = ombra(100.05)
    confronti = compare(real_fills(store), theoretical_fills(risultato), aperture())
    sommario = summarize(
        confronti,
        live_equity=live_equity_series(store),
        shadow_equity=risultato.equity_series(),
        generated_at=date(2026, 9, 9),
    )

    assert sommario.days_compared == 2
    assert sommario.tracking_error_cumulative_bps == pytest.approx(-10.0, abs=0.01)
    assert sommario.slippage_ratio == pytest.approx(10.0)
    assert list(sommario.per_symbol["symbol"]) == ["SPY"]

    dettaglio, storico = write_report(confronti, sommario, tmp_path, date(2026, 9, 9))
    assert dettaglio.name == "reconcile_2026-09-09.csv"
    righe = pd.read_csv(dettaglio)
    assert righe["slippage_real_bps"].iloc[0] == pytest.approx(50.0)

    write_report(confronti, sommario, tmp_path, date(2026, 9, 10))
    storia = pd.read_csv(storico)
    assert len(storia) == 2
    assert storia["n_matched"].iloc[0] == 1


def test_aperture_di_riferimento_dai_parquet(spy_parquet_dir: Path) -> None:
    aperture_reali = reference_opens(["SPY", "MANCANTE"], spy_parquet_dir)
    barre = pd.read_parquet(spy_parquet_dir / "SPY.parquet")
    assert set(aperture_reali) == {"SPY"}
    assert aperture_reali["SPY"].iloc[0] == barre["open"].iloc[0]


def test_eseguiti_reali_con_slippage_in_valuta(store: StateStore) -> None:
    """Cento azioni pagate mezzo dollaro sopra l'apertura: cinquanta dollari di slippage."""
    popola(store, prezzo=100.5)
    eventi = real_fill_events(store, aperture())
    assert len(eventi) == 1
    assert eventi[0].slippage_cost == pytest.approx(50.0)
    assert eventi[0].commission == 1.0
    assert real_fill_events(store)[0].slippage_cost == 0.0


def test_esposizione_live_dallo_stato(store: StateStore) -> None:
    store.save_equity(date(2026, 9, 7), 1_000.0, 9_000.0)
    store.save_equity(date(2026, 9, 8), 5_000.0, 5_000.0)
    assert list(live_exposure_series(store)) == pytest.approx([0.9, 0.5])


def test_record_senza_segnale_non_calcola_il_ritardo() -> None:
    record = FillRecord(GIORNO, "SPY", "BUY", 10, 100.0, 0.0)
    assert record.delay_days is None
    assert record.day == date(2026, 9, 8)
