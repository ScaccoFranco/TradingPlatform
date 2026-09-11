"""Test del runner live e dello scheduler, tutto su componenti finti."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from fake_alpaca import TradingClientFinto

from quant.brokers.alpaca import AlpacaExecutionHandler, client_order_id
from quant.data import ParquetDataHandler
from quant.events import FillEvent, MarketEvent, OrderDirection, SignalDirection, SignalEvent
from quant.live.runner import LiveRunner
from quant.live.schedule import (
    ORA_RICONCILIAZIONE,
    ORA_TRADING,
    build_scheduler,
    is_trading_day,
    prossime_esecuzioni,
    solo_se_borsa_aperta,
    trading_trigger,
)
from quant.portfolio import Portfolio
from quant.risk import KillSwitch, RiskManager
from quant.state import StateStore
from quant.strategies.momentum import CrossSectionalMomentum
from quant.strategy import Strategy

SIMBOLI = ["AAA", "BBB", "CCC", "DDD", "EEE", "SPY", "SHY"]
UNIVERSO = SIMBOLI[:5]
CAPITALE = 100_000.0


class EsecuzioneSpia:
    """Execution handler finto che registra l'ordine delle chiamate."""

    def __init__(
        self, tracce: list[str], cash: float = CAPITALE, positions: dict[str, int] | None = None
    ) -> None:
        self.tracce = tracce
        self.cash = cash
        self.positions = dict(positions or {})
        self.ordini: list = []
        self.fills: list[FillEvent] = []
        self.esplode = False

    def get_cash(self) -> float:
        self.tracce.append("get_cash")
        return self.cash

    def on_market(self, event: MarketEvent) -> list[FillEvent]:
        self.tracce.append("on_market")
        pronti, self.fills = self.fills, []
        return pronti

    def on_order(self, order) -> None:
        self.tracce.append("on_order")
        if self.esplode:
            raise RuntimeError("broker irraggiungibile")
        self.ordini.append(order)

    def get_positions(self) -> dict[str, int]:
        self.tracce.append("get_positions")
        return dict(self.positions)


class StrategiaSpia(Strategy):
    """Emette un segnale fisso e registra quando viene interrogata."""

    def __init__(self, tracce: list[str], segnali: list[SignalEvent] | None = None) -> None:
        super().__init__(None)
        self.tracce = tracce
        self.segnali = segnali

    def on_bar(self, event: MarketEvent) -> list[SignalEvent]:
        self.tracce.append("on_bar")
        if self.segnali is not None:
            return self.segnali
        return [SignalEvent(event.timestamp, "AAA", SignalDirection.LONG, 0.5)]


def costruisci(
    tmp_path: Path, directory: Path, tracce: list[str], **kwargs
) -> tuple[LiveRunner, EsecuzioneSpia]:
    """Runner con dati sintetici, portafoglio e rischio veri, esecuzione finta."""
    handler = ParquetDataHandler(directory, SIMBOLI)
    esecuzione = EsecuzioneSpia(tracce, **kwargs)
    interruttore = KillSwitch(path=tmp_path / "KILL")
    runner = LiveRunner(
        strategy=StrategiaSpia(tracce),
        portfolio=Portfolio(handler, initial_cash=CAPITALE, cash_buffer=0.01),
        risk_manager=RiskManager(max_weight_per_symbol=0.5, kill_switch=interruttore),
        execution_handler=esecuzione,
        data_handler=handler,
        state_store=StateStore(tmp_path / "live.db"),
        kill_switch=interruttore,
    )
    return runner, esecuzione


def test_le_fasi_girano_nellordine_giusto(tmp_path: Path, momentum_parquet_dir: Path) -> None:
    tracce: list[str] = []
    runner, esecuzione = costruisci(tmp_path, momentum_parquet_dir, tracce)
    sommario = runner.run_once()

    assert tracce.index("get_cash") < tracce.index("on_market") < tracce.index("on_bar")
    assert tracce.index("on_bar") < tracce.index("on_order") < tracce.index("get_positions")
    assert len(sommario.signals) == 1
    assert len(esecuzione.ordini) == 1
    assert sommario.submitted[0].symbol == "AAA"


def test_lo_stato_viene_persistito_e_riletto(tmp_path: Path, momentum_parquet_dir: Path) -> None:
    tracce: list[str] = []
    runner, esecuzione = costruisci(tmp_path, momentum_parquet_dir, tracce)
    esecuzione.fills = [FillEvent(datetime(2015, 2, 2), "AAA", OrderDirection.BUY, 100, 120.0, 0.0)]
    sommario = runner.run_once()

    assert sommario.fills
    store = StateStore(tmp_path / "live.db")
    assert store.load_positions()["AAA"] == 100
    assert len(store.load_equity(30)) == 1

    ripartito, _ = costruisci(tmp_path, momentum_parquet_dir, [])
    ripartito.run_once()
    assert ripartito.portfolio.positions["AAA"] == 100


def test_la_riconciliazione_segnala_lo_scarto(tmp_path: Path, momentum_parquet_dir: Path) -> None:
    tracce: list[str] = []
    runner, _ = costruisci(tmp_path, momentum_parquet_dir, tracce, positions={"AAA": 7})
    sommario = runner.run_once()

    assert [(m.symbol, m.expected, m.actual) for m in sommario.mismatches] == [("AAA", 0, 7)]
    assert runner.state_store.load_positions() == {}


def test_eccezione_attiva_il_kill_switch_e_non_ritenta(tmp_path: Path, momentum_parquet_dir: Path) -> None:
    tracce: list[str] = []
    runner, esecuzione = costruisci(tmp_path, momentum_parquet_dir, tracce)
    esecuzione.esplode = True

    with pytest.raises(RuntimeError, match="broker irraggiungibile"):
        runner.run_once()

    assert runner.kill_switch is not None
    assert runner.kill_switch.is_active()
    assert "eccezione in run_once" in runner.kill_switch.reason()
    assert tracce.count("on_order") == 1


def test_col_kill_switch_attivo_nessun_ordine_esce(tmp_path: Path, momentum_parquet_dir: Path) -> None:
    tracce: list[str] = []
    runner, esecuzione = costruisci(tmp_path, momentum_parquet_dir, tracce)
    assert runner.kill_switch is not None
    runner.kill_switch.activate("prova")

    sommario = runner.run_once()
    assert sommario.submitted == []
    assert esecuzione.ordini == []
    assert sommario.decisions[0].reason == "KILL_SWITCH"


def test_reconcile_now_non_invia_ordini(tmp_path: Path, momentum_parquet_dir: Path) -> None:
    tracce: list[str] = []
    runner, esecuzione = costruisci(tmp_path, momentum_parquet_dir, tracce, positions={"BBB": 3})
    differenze = runner.reconcile_now()

    assert [d.symbol for d in differenze] == ["BBB"]
    assert esecuzione.ordini == []
    assert "on_bar" not in tracce


def test_momentum_live_manda_ordini_al_broker(tmp_path: Path, momentum_parquet_dir: Path) -> None:
    """Le classi del backtest, con l'esecuzione Alpaca finta al posto di quella simulata.

    La finestra si chiude sul primo giorno di novembre: e' la barra in cui il momentum
    ribilancia, cioe' l'unico giorno del mese in cui in live esce un ordine.
    """
    handler = ParquetDataHandler(momentum_parquet_dir, SIMBOLI, end="2016-11-01")
    client = TradingClientFinto(cash=CAPITALE)
    store = StateStore(tmp_path / "live.db")
    esecuzione = AlpacaExecutionHandler(client, strategy_name="momentum_test", state_store=store)
    runner = LiveRunner(
        strategy=CrossSectionalMomentum(handler, UNIVERSO, top_n=2, cash_symbol="SHY"),
        portfolio=Portfolio(handler, initial_cash=CAPITALE, cash_buffer=0.01),
        risk_manager=RiskManager(max_weight_per_symbol=0.5, allowed_symbols=SIMBOLI),
        execution_handler=esecuzione,
        data_handler=handler,
        state_store=store,
    )
    sommario = runner.run_once()

    assert {s.symbol for s in sommario.signals} == {"AAA", "BBB"}
    assert len(client.inviati) == len(sommario.submitted) == 2
    identificativo = client_order_id(sommario.timestamp.date(), "AAA", "momentum_test")
    assert store.order_exists(identificativo)


def test_i_trigger_non_scattano_nel_fine_settimana() -> None:
    for ora in (ORA_TRADING, ORA_RICONCILIAZIONE):
        scatti = prossime_esecuzioni(trading_trigger(*ora), 15)
        assert scatti
        assert all(s.weekday() < 5 for s in scatti)
        assert {(s.hour, s.minute) for s in scatti} == {ora}


def test_lo_scheduler_registra_i_due_job() -> None:
    calendario = SimpleNamespace(get_calendar=lambda request: [])
    scheduler = build_scheduler(calendario, lambda: None, lambda: None, scheduler=BackgroundScheduler())
    identificativi = {job.id for job in scheduler.get_jobs()}
    assert identificativi == {"trading", "riconciliazione"}


def test_il_job_salta_i_giorni_di_chiusura() -> None:
    chiuso = SimpleNamespace(get_calendar=lambda request: [])
    aperto = SimpleNamespace(get_calendar=lambda request: [SimpleNamespace(date=date.today())])
    eseguiti: list[str] = []

    solo_se_borsa_aperta(chiuso, lambda: eseguiti.append("chiuso"))()
    assert eseguiti == []

    solo_se_borsa_aperta(aperto, lambda: eseguiti.append("aperto"))()
    assert eseguiti == ["aperto"]


def test_calendario_irraggiungibile_ripiega_sui_feriali() -> None:
    def esplode(request):
        raise RuntimeError("rete assente")

    rotto = SimpleNamespace(get_calendar=esplode)
    assert is_trading_day(rotto, date(2026, 9, 11)) is True
    assert is_trading_day(rotto, date(2026, 9, 12)) is False
