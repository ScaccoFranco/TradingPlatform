"""Test dell'adapter Alpaca con client finti: nessuna chiamata di rete."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest
from fake_alpaca import DataClientFinto, TradingClientFinto

from quant.brokers.alpaca import AlpacaExecutionHandler, client_order_id
from quant.brokers.alpaca_data import AlpacaDataHandler
from quant.data import COLUMNS
from quant.events import MarketEvent, OrderDirection, OrderEvent
from quant.state import StateStore, reconcile

MOMENTO = datetime(2026, 9, 10, 15, 45)


def ordine(
    symbol: str = "SPY", quantita: int = 10, direzione: OrderDirection = OrderDirection.BUY
) -> OrderEvent:
    """Ordine di comodo per i test."""
    return OrderEvent(MOMENTO, symbol, direzione, quantita)


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "live.db")


def test_client_order_id_e_deterministico() -> None:
    primo = client_order_id(date(2026, 9, 10), "SPY", "momentum")
    assert primo == client_order_id(date(2026, 9, 10), "SPY", "momentum")
    assert primo != client_order_id(date(2026, 9, 11), "SPY", "momentum")
    assert primo != client_order_id(date(2026, 9, 10), "QQQ", "momentum")
    assert primo != client_order_id(date(2026, 9, 10), "SPY", "mean_reversion")


def test_ordine_inviato_due_volte_non_si_duplica(store: StateStore) -> None:
    client = TradingClientFinto()
    handler = AlpacaExecutionHandler(client, strategy_name="momentum", state_store=store)

    assert handler.submit_order(ordine()) is not None
    assert handler.submit_order(ordine()) is None
    assert len(client.inviati) == 1


def test_il_riavvio_non_riemette_lo_stesso_ordine(store: StateStore) -> None:
    """Processo ripartito, memoria vuota: il doppione lo riconosce il broker."""
    client = TradingClientFinto()
    AlpacaExecutionHandler(client, strategy_name="momentum", state_store=store).submit_order(ordine())

    dopo_riavvio = AlpacaExecutionHandler(client, strategy_name="momentum")
    assert dopo_riavvio.submit_order(ordine()) is None
    assert len(client.inviati) == 1


def test_ordine_market_e_limit(store: StateStore) -> None:
    client = TradingClientFinto()
    market = AlpacaExecutionHandler(client, strategy_name="a", state_store=store)
    market.submit_order(ordine())
    assert client.inviati[0].type.value == "market"

    limite = AlpacaExecutionHandler(client, strategy_name="b")
    limite.submit_order(ordine(), limit_price=123.45)
    assert client.inviati[1].type.value == "limit"
    assert float(client.inviati[1].limit_price) == 123.45


def test_fill_convertito_una_volta_sola(store: StateStore) -> None:
    client = TradingClientFinto()
    handler = AlpacaExecutionHandler(client, strategy_name="momentum", state_store=store)
    handler.submit_order(ordine())
    assert handler.collect_fills() == []

    identificativo = client_order_id(MOMENTO.date(), "SPY", "momentum")
    client.per_client_id[identificativo].esegui(prezzo=650.25, quando=MOMENTO)

    fills = handler.on_market(MarketEvent(MOMENTO))
    assert len(fills) == 1
    assert fills[0].symbol == "SPY"
    assert fills[0].quantity == 10
    assert fills[0].fill_price == 650.25
    assert handler.collect_fills() == []
    assert store.fill_exists(identificativo)


def test_lettura_conto_e_annullamento() -> None:
    client = TradingClientFinto(cash=12_345.67, positions={"SPY": 10, "IEF": -3})
    handler = AlpacaExecutionHandler(client, strategy_name="momentum")
    assert handler.get_cash() == 12_345.67
    assert handler.get_positions() == {"SPY": 10, "IEF": -3}

    risposta = handler.submit_order(ordine())
    assert len(handler.get_open_orders()) == 1
    handler.cancel_order(risposta.id)
    assert client.annullati == [risposta.id]
    assert handler.get_open_orders() == []


def test_riconciliazione_rileva_lo_scarto_senza_correggere(store: StateStore) -> None:
    store.save_positions({"SPY": 10, "IEF": 5})
    client = TradingClientFinto(positions={"SPY": 8, "GLD": 2})
    handler = AlpacaExecutionHandler(client, strategy_name="momentum", state_store=store)

    differenze = reconcile(store.load_positions(), handler.get_positions())
    per_simbolo = {d.symbol: d for d in differenze}
    assert set(per_simbolo) == {"SPY", "IEF", "GLD"}
    assert per_simbolo["SPY"].expected == 10 and per_simbolo["SPY"].actual == 8
    assert per_simbolo["GLD"].delta == 2
    assert store.load_positions() == {"SPY": 10, "IEF": 5}
    assert handler.get_positions() == {"SPY": 8, "GLD": 2}


def test_riconciliazione_silenziosa_quando_tutto_torna(store: StateStore) -> None:
    store.save_positions({"SPY": 10})
    client = TradingClientFinto(positions={"SPY": 10})
    handler = AlpacaExecutionHandler(client, strategy_name="momentum", state_store=store)
    assert reconcile(store.load_positions(), handler.get_positions()) == []


def barre_finte(giorni: int = 30, base: float = 100.0) -> pd.DataFrame:
    """Barre daily sintetiche nel formato che restituisce l'SDK."""
    date_range = pd.bdate_range("2026-08-03", periods=giorni)
    closes = [base + i for i in range(giorni)]
    return pd.DataFrame(
        {
            "open": [c - 0.5 for c in closes],
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [1_000_000.0] * giorni,
            "trade_count": [1000.0] * giorni,
            "vwap": closes,
        },
        index=date_range,
    )


def test_data_handler_live_espone_la_stessa_interfaccia() -> None:
    client = DataClientFinto({"SPY": barre_finte(), "QQQ": barre_finte(30, 200.0)})
    handler = AlpacaDataHandler(client, ["SPY", "QQQ"], start="2026-08-01", end="2026-09-30")

    assert list(handler.get_latest_bars_all(1)) == ["SPY", "QQQ"]
    assert handler.current_timestamp() is None

    for _ in range(5):
        handler.update_bars()
    barre = handler.get_latest_bars("SPY", 3)
    assert list(barre.columns) == list(COLUMNS)
    assert len(barre) == 3
    assert barre.index.max() == pd.Timestamp(handler.current_timestamp())
    limite = pd.Timestamp(handler.current_timestamp())
    assert all(f.index.max() <= limite for f in handler.get_latest_bars_all(10).values())


def test_adj_close_viene_dalle_barre_rettificate() -> None:
    client = DataClientFinto({"SPY": barre_finte()}, fattore_rettifica=0.9)
    handler = AlpacaDataHandler(client, ["SPY"], start="2026-08-01", end="2026-09-30")
    handler.advance_to_latest()
    barre = handler.get_latest_bars("SPY", 1)
    assert barre["adj_close"].iloc[-1] == pytest.approx(barre["close"].iloc[-1] * 0.9)
    assert {r.adjustment.value for r in client.richieste} == {"raw", "all"}


def test_advance_to_latest_arriva_allultima_barra() -> None:
    client = DataClientFinto({"SPY": barre_finte(12)})
    handler = AlpacaDataHandler(client, ["SPY"], start="2026-08-01", end="2026-09-30")
    evento = handler.advance_to_latest()
    assert evento is not None
    assert pd.Timestamp(evento.timestamp) == handler.timeline[-1]
    assert handler.continue_backtest is False


def test_i_fill_del_giorno_prima_arrivano_dopo_il_riavvio(store: StateStore) -> None:
    """Lo scheduler ricrea l'handler a ogni run: gli ordini pendenti vivono nel database."""
    client = TradingClientFinto()
    AlpacaExecutionHandler(client, strategy_name="momentum", state_store=store).submit_order(ordine())
    identificativo = client_order_id(MOMENTO.date(), "SPY", "momentum")
    client.per_client_id[identificativo].esegui(prezzo=651.0, quando=MOMENTO)

    dopo_riavvio = AlpacaExecutionHandler(client, strategy_name="momentum", state_store=store)
    assert dopo_riavvio.submitted == {}
    fills = dopo_riavvio.collect_fills()
    assert len(fills) == 1
    assert fills[0].fill_price == 651.0
    assert store.recent_orders()[0].status == "filled"
    assert dopo_riavvio.collect_fills() == []


def test_ordine_annullato_smette_di_essere_pendente(store: StateStore) -> None:
    client = TradingClientFinto()
    handler = AlpacaExecutionHandler(client, strategy_name="momentum", state_store=store)
    risposta = handler.submit_order(ordine())
    handler.cancel_order(risposta.id)

    assert handler.collect_fills() == []
    assert store.recent_orders()[0].status == "canceled"
    assert store.pending_orders() == []
