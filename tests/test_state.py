"""Test dello stato persistito e della configurazione live."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from quant.config import ConfigError, Settings
from quant.events import FillEvent, OrderDirection, OrderEvent
from quant.state import StateStore, reconcile

MOMENTO = datetime(2026, 9, 10, 15, 45)


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "live.db")


def ordine(symbol: str = "SPY", quantita: int = 10) -> OrderEvent:
    return OrderEvent(MOMENTO, symbol, OrderDirection.BUY, quantita)


def test_ordine_registrato_una_volta_sola(store: StateStore) -> None:
    store.record_order("id-1", ordine())
    store.record_order("id-1", ordine(), status="submitted", broker_order_id="b-1")
    assert store.order_exists("id-1")
    assert not store.order_exists("id-2")

    ordini = store.recent_orders()
    assert len(ordini) == 1
    assert ordini[0].status == "submitted"
    assert ordini[0].broker_order_id == "b-1"


def test_stato_ordine_aggiornabile(store: StateStore) -> None:
    store.record_order("id-1", ordine())
    store.update_order_status("id-1", "filled", "b-9")
    assert store.recent_orders()[0].status == "filled"
    assert store.recent_orders()[0].broker_order_id == "b-9"


def test_fill_non_contato_due_volte(store: StateStore) -> None:
    fill = FillEvent(MOMENTO, "SPY", OrderDirection.BUY, 10, 650.0, 0.0)
    assert store.record_fill("id-1", fill) is True
    assert store.record_fill("id-1", fill) is False
    assert len(store.load_fills()) == 1
    assert store.load_fills()[0].fill_price == 650.0


def test_posizioni_sopravvivono_al_riavvio(tmp_path: Path) -> None:
    percorso = tmp_path / "live.db"
    primo = StateStore(percorso)
    primo.save_positions({"SPY": 10, "IEF": 4, "GLD": 0})
    primo.close()

    dopo_riavvio = StateStore(percorso)
    assert dopo_riavvio.load_positions() == {"SPY": 10, "IEF": 4}


def test_equity_giornaliera_in_ordine(store: StateStore) -> None:
    store.save_equity(date(2026, 9, 8), 1_000.0, 9_000.0)
    store.save_equity(date(2026, 9, 9), 500.0, 9_800.0)
    store.save_equity(date(2026, 9, 9), 500.0, 9_900.0)

    serie = store.load_equity(30)
    assert [g for g, _, _, _ in serie] == [date(2026, 9, 8), date(2026, 9, 9)]
    assert serie[-1][3] == 10_400.0
    assert len(store.load_equity(1)) == 1


def test_riconciliazione_elenca_solo_le_differenze() -> None:
    differenze = reconcile({"SPY": 10, "IEF": 5}, {"SPY": 10, "IEF": 4})
    assert len(differenze) == 1
    assert differenze[0].symbol == "IEF"
    assert differenze[0].delta == -1


def test_paper_obbligatorio() -> None:
    with pytest.raises(ConfigError, match="ALPACA_PAPER"):
        Settings(alpaca_api_key="k", alpaca_secret_key="s", alpaca_paper=False).validate_live()


def test_chiavi_obbligatorie() -> None:
    with pytest.raises(ConfigError, match="ALPACA_SECRET_KEY"):
        Settings(alpaca_api_key="k", alpaca_secret_key="").validate_live()


def test_configurazione_valida_e_alert_opzionali() -> None:
    impostazioni = Settings(alpaca_api_key="k", alpaca_secret_key="s", alpaca_paper=True)
    assert impostazioni.validate_live() is impostazioni
    assert impostazioni.telegram_enabled is False
    completo = Settings(
        alpaca_api_key="k", alpaca_secret_key="s", telegram_bot_token="t", telegram_chat_id="c"
    )
    assert completo.telegram_enabled
