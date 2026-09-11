"""Test del RiskManager: peso massimo, esposizione lorda, blocco su drawdown."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from quant.data import ParquetDataHandler
from quant.events import OrderDirection, OrderEvent, SignalDirection, SignalEvent
from quant.portfolio import Portfolio
from quant.risk import KillSwitch, RiskManager, RiskReason

CAPITALE = 100_000.0


def contesto(directory: Path) -> tuple[ParquetDataHandler, Portfolio]:
    """Handler posizionato sulla prima barra e portafoglio con equity segnata."""
    handler = ParquetDataHandler(directory, ["SPY"])
    handler.update_bars()
    portfolio = Portfolio(handler, initial_cash=CAPITALE)
    portfolio.equity_curve.append((datetime(2020, 1, 1), CAPITALE))
    return handler, portfolio


def ordine_pieno(portfolio: Portfolio) -> OrderEvent:
    """Ordine generato da un segnale LONG a forza piena."""
    segnale = SignalEvent(datetime(2020, 1, 1), "SPY", SignalDirection.LONG, 1.0)
    ordine = portfolio.on_signal(segnale)
    assert ordine is not None
    return ordine


def test_peso_massimo_dimezza_la_quantita(spy_parquet_dir: Path) -> None:
    _, portfolio = contesto(spy_parquet_dir)
    ordine = ordine_pieno(portfolio)
    filtrato = RiskManager(max_weight_per_symbol=0.5).filter(ordine, portfolio)
    assert filtrato is not None
    prezzo = portfolio.last_price("SPY")
    assert prezzo is not None
    assert filtrato.quantity == int(0.5 * CAPITALE // prezzo)
    assert filtrato.quantity * prezzo / CAPITALE <= 0.5


def test_ordine_entro_i_limiti_passa_invariato(spy_parquet_dir: Path) -> None:
    _, portfolio = contesto(spy_parquet_dir)
    ordine = ordine_pieno(portfolio)
    assert RiskManager(max_weight_per_symbol=1.0).filter(ordine, portfolio) is ordine


def test_esposizione_lorda_rifiuta_lordine(spy_parquet_dir: Path) -> None:
    _, portfolio = contesto(spy_parquet_dir)
    ordine = ordine_pieno(portfolio)
    manager = RiskManager(max_weight_per_symbol=1.0, max_gross_exposure=0.3)
    assert manager.filter(ordine, portfolio) is None


def test_drawdown_oltre_soglia_rifiuta_lordine(spy_parquet_dir: Path) -> None:
    _, portfolio = contesto(spy_parquet_dir)
    ordine = ordine_pieno(portfolio)
    portfolio.equity_curve.append((datetime(2020, 1, 2), CAPITALE * 0.75))
    manager = RiskManager(max_drawdown=0.20)
    assert manager.current_drawdown(portfolio) == 0.25
    assert manager.filter(ordine, portfolio) is None


def test_le_chiusure_passano_anche_in_drawdown(spy_parquet_dir: Path) -> None:
    """Il blocco vale per chi aumenta il rischio, mai per chi lo riduce."""
    _, portfolio = contesto(spy_parquet_dir)
    portfolio.positions["SPY"] = 500
    portfolio.equity_curve.append((datetime(2020, 1, 2), CAPITALE * 0.5))
    vendita = OrderEvent(datetime(2020, 1, 2), "SPY", OrderDirection.SELL, 500)
    assert RiskManager(max_drawdown=0.10).filter(vendita, portfolio) is vendita


def test_whitelist_rifiuta_i_simboli_fuori_elenco(spy_parquet_dir: Path) -> None:
    _, portfolio = contesto(spy_parquet_dir)
    ordine = ordine_pieno(portfolio)
    decisione = RiskManager(allowed_symbols=["QQQ", "IEF"]).decide(ordine, portfolio)
    assert decisione.rejected
    assert decisione.reason == RiskReason.NOT_WHITELISTED
    assert RiskManager(allowed_symbols=["SPY"]).decide(ordine, portfolio).order_final is ordine


def test_quota_giornaliera_di_ordini(spy_parquet_dir: Path) -> None:
    _, portfolio = contesto(spy_parquet_dir)
    manager = RiskManager(max_orders_per_day=2)
    piccolo = OrderEvent(datetime(2020, 1, 1), "SPY", OrderDirection.BUY, 1)

    assert manager.filter(piccolo, portfolio) is piccolo
    assert manager.filter(piccolo, portfolio) is piccolo
    terza = manager.decide(piccolo, portfolio)
    assert terza.rejected
    assert terza.reason == RiskReason.MAX_ORDERS_PER_DAY
    assert manager.orders_today(date(2020, 1, 1)) == 2

    domani = OrderEvent(datetime(2020, 1, 2), "SPY", OrderDirection.BUY, 1)
    assert manager.filter(domani, portfolio) is domani


def test_gli_ordini_rifiutati_non_consumano_la_quota(spy_parquet_dir: Path) -> None:
    _, portfolio = contesto(spy_parquet_dir)
    manager = RiskManager(max_orders_per_day=1, allowed_symbols=["QQQ"])
    fuori = OrderEvent(datetime(2020, 1, 1), "SPY", OrderDirection.BUY, 1)
    assert manager.decide(fuori, portfolio).rejected
    assert manager.orders_today(date(2020, 1, 1)) == 0


def test_taglio_massimo_per_ordine(spy_parquet_dir: Path) -> None:
    _, portfolio = contesto(spy_parquet_dir)
    ordine = ordine_pieno(portfolio)
    prezzo = portfolio.last_price("SPY")
    assert prezzo is not None

    decisione = RiskManager(max_notional_per_order=10_000.0).decide(ordine, portfolio)
    assert decisione.reduced
    assert decisione.reason == RiskReason.MAX_NOTIONAL_PER_ORDER
    assert decisione.order_final is not None
    assert decisione.order_final.quantity == int(10_000.0 // prezzo)
    assert decisione.order_final.quantity * prezzo <= 10_000.0


def test_kill_switch_blocca_qualunque_ordine(tmp_path: Path, spy_parquet_dir: Path) -> None:
    _, portfolio = contesto(spy_parquet_dir)
    portfolio.positions["SPY"] = 100
    interruttore = KillSwitch(path=tmp_path / "KILL")
    manager = RiskManager(kill_switch=interruttore)
    acquisto = OrderEvent(datetime(2020, 1, 1), "SPY", OrderDirection.BUY, 10)
    vendita = OrderEvent(datetime(2020, 1, 1), "SPY", OrderDirection.SELL, 100)

    assert manager.filter(acquisto, portfolio) is acquisto
    interruttore.activate("test manuale")
    assert (tmp_path / "KILL").exists()
    assert interruttore.reason() == "test manuale"
    assert manager.decide(acquisto, portfolio).reason == RiskReason.KILL_SWITCH
    assert manager.decide(vendita, portfolio).rejected

    interruttore.deactivate()
    assert not (tmp_path / "KILL").exists()
    assert manager.filter(vendita, portfolio) is vendita


def test_kill_switch_dal_file_esistente(tmp_path: Path) -> None:
    percorso = tmp_path / "KILL"
    percorso.write_text("drawdown oltre soglia\n")
    interruttore = KillSwitch(path=percorso)
    assert interruttore.is_active()
    assert interruttore.reason() == "drawdown oltre soglia"


def test_regola_piu_restrittiva_quando_si_sommano(spy_parquet_dir: Path) -> None:
    """Peso massimo e taglio per ordine insieme: vince il piu' stretto dei due."""
    _, portfolio = contesto(spy_parquet_dir)
    ordine = ordine_pieno(portfolio)
    prezzo = portfolio.last_price("SPY")
    assert prezzo is not None

    manager = RiskManager(max_weight_per_symbol=0.5, max_notional_per_order=5_000.0)
    decisione = manager.decide(ordine, portfolio)
    assert decisione.order_final is not None
    assert decisione.order_final.quantity == int(5_000.0 // prezzo)
    assert RiskReason.MAX_WEIGHT_PER_SYMBOL in decisione.reason
    assert RiskReason.MAX_NOTIONAL_PER_ORDER in decisione.reason


def test_ogni_ordine_lascia_una_decisione(spy_parquet_dir: Path) -> None:
    """Nessun ordine viene scartato in silenzio: una decisione per chiamata."""
    _, portfolio = contesto(spy_parquet_dir)
    manager = RiskManager(max_weight_per_symbol=0.5, allowed_symbols=["SPY"])
    ordine = ordine_pieno(portfolio)
    manager.filter(ordine, portfolio)
    manager.filter(OrderEvent(datetime(2020, 1, 1), "TLT", OrderDirection.BUY, 5), portfolio)

    assert len(manager.decisions) == 2
    assert manager.decisions[0].reduced
    assert manager.decisions[1].rejected
    assert all(d.order_original is not None for d in manager.decisions)


def test_drawdown_dal_picco_registrato(spy_parquet_dir: Path) -> None:
    """Il picco incrementale da' lo stesso numero della scansione dell'equity curve."""
    _, portfolio = contesto(spy_parquet_dir)
    valori = [CAPITALE, CAPITALE * 1.2, CAPITALE * 1.1, CAPITALE * 0.9]
    portfolio.equity_curve.clear()
    for i, valore in enumerate(valori):
        portfolio.equity_curve.append((datetime(2020, 1, i + 1), valore))
        portfolio.peak_equity = max(portfolio.peak_equity, valore)

    manuale = (max(valori) - valori[-1]) / max(valori)
    assert RiskManager().current_drawdown(portfolio) == pytest.approx(manuale)
    assert portfolio.peak_equity == max(valori)


def test_il_picco_si_aggiorna_al_mark_to_market(spy_parquet_dir: Path) -> None:
    handler = ParquetDataHandler(spy_parquet_dir, ["SPY"])
    portfolio = Portfolio(handler, initial_cash=CAPITALE)
    assert portfolio.peak_equity == CAPITALE
    for _ in range(3):
        portfolio.on_market(handler.update_bars()[0])
    assert portfolio.peak_equity == max(v for _, v in portfolio.equity_curve)


def test_drawdown_nullo_senza_equity_curve(spy_parquet_dir: Path) -> None:
    handler = ParquetDataHandler(spy_parquet_dir, ["SPY"])
    assert RiskManager().current_drawdown(Portfolio(handler, initial_cash=CAPITALE)) == 0.0


def test_ogni_motivo_di_rifiuto_ha_la_sua_reason(spy_parquet_dir: Path) -> None:
    """I quattro motivi del filtro di portafoglio, ciascuno in isolamento."""
    _, portfolio = contesto(spy_parquet_dir)
    ordine = ordine_pieno(portfolio)

    peso = RiskManager(max_weight_per_symbol=0.5).decide(ordine, portfolio)
    assert peso.reduced and peso.reason == RiskReason.MAX_WEIGHT_PER_SYMBOL

    esposizione = RiskManager(max_gross_exposure=0.1).decide(ordine, portfolio)
    assert esposizione.rejected and esposizione.reason == RiskReason.MAX_GROSS_EXPOSURE

    portfolio.equity_curve.append((datetime(2020, 1, 2), CAPITALE * 0.5))
    drawdown = RiskManager(max_drawdown=0.1).decide(ordine, portfolio)
    assert drawdown.rejected and drawdown.reason == RiskReason.MAX_DRAWDOWN

    vergine = ParquetDataHandler(spy_parquet_dir, ["SPY"])
    senza_prezzo = Portfolio(vergine, initial_cash=CAPITALE)
    decisione = RiskManager().decide(ordine, senza_prezzo)
    assert decisione.rejected and decisione.reason == RiskReason.NO_PRICE


def test_una_decisione_per_ogni_ordine_del_backtest(spy_parquet_dir: Path) -> None:
    """Dopo un backtest le decisioni registrate sono quante gli ordini entrati nel filtro."""
    from quant.engine import Backtest
    from quant.execution import SimulatedExecutionHandler
    from quant.strategy import BuyAndHoldStrategy

    handler = ParquetDataHandler(spy_parquet_dir, ["SPY"])
    portfolio = Portfolio(handler, initial_cash=CAPITALE)
    manager = RiskManager(max_weight_per_symbol=0.5)
    backtest = Backtest(
        handler,
        BuyAndHoldStrategy(handler, "SPY"),
        portfolio,
        manager,
        SimulatedExecutionHandler(handler),
    )
    backtest.run()

    assert backtest.order_events == 1
    assert len(manager.decisions) == backtest.order_events
    assert manager.decisions[0].reduced
    assert all(d.order_original is not None for d in manager.decisions)
