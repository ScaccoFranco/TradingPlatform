"""Avvia lo scheduler del paper trading: un run al giorno, piu' la riconciliazione."""

from __future__ import annotations

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.trading.client import TradingClient

from quant.brokers.alpaca import AlpacaExecutionHandler
from quant.brokers.alpaca_data import AlpacaDataHandler
from quant.config import Settings, load_settings
from quant.live.alerts import build_alerter
from quant.live.deployment import CASH, SIMBOLI, STRATEGIA, UNIVERSO
from quant.live.runner import LiveRunner
from quant.live.schedule import build_scheduler
from quant.logging import configure_for_live, get_logger
from quant.portfolio import Portfolio
from quant.risk import KillSwitch, RiskManager
from quant.state import StateStore
from quant.strategies.momentum import CrossSectionalMomentum
from quant.weekly import generate_weekly_report

logger = get_logger("live.main")

CAPITALE_ATTESO = 100_000.0
GIORNI_DI_STORIA = 600

LIMITI = {
    "max_weight_per_symbol": 0.4,
    "max_gross_exposure": 1.0,
    "max_drawdown": 0.25,
    "max_orders_per_day": 8,
    "max_notional_per_order": 50_000.0,
    "allowed_symbols": SIMBOLI,
}


def costruisci_runner(settings: Settings, kill_switch: KillSwitch, store: StateStore) -> LiveRunner:
    """Ricostruisce dati e componenti a ogni esecuzione: le barre devono essere fresche."""
    trading_client = TradingClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        paper=settings.alpaca_paper,
    )
    data_client = StockHistoricalDataClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
    )
    data_handler = AlpacaDataHandler(data_client, SIMBOLI, lookback_days=GIORNI_DI_STORIA)
    alerter = build_alerter(settings)
    execution = AlpacaExecutionHandler(
        trading_client,
        strategy_name=STRATEGIA,
        state_store=store,
        data_handler=data_handler,
    )
    return LiveRunner(
        strategy=CrossSectionalMomentum(data_handler, UNIVERSO, cash_symbol=CASH),
        portfolio=Portfolio(data_handler, initial_cash=CAPITALE_ATTESO, cash_buffer=0.01),
        risk_manager=RiskManager(kill_switch=kill_switch, **LIMITI),
        execution_handler=execution,
        data_handler=data_handler,
        state_store=store,
        kill_switch=kill_switch,
        alerter=alerter,
    )


def main() -> None:
    """Valida la configurazione, registra i job e resta in ascolto."""
    file_di_log = configure_for_live()
    settings = load_settings().validate_live()
    logger.info("log_su_file", path=str(file_di_log))
    kill_switch = KillSwitch()
    store = StateStore()

    if kill_switch.is_active():
        logger.error("kill_switch_attivo_allavvio", reason=kill_switch.reason())

    def trading_job() -> None:
        """Job serale: genera e invia gli ordini della giornata."""
        if kill_switch.is_active():
            logger.error("run_saltata_per_kill_switch", reason=kill_switch.reason())
            return
        costruisci_runner(settings, kill_switch, store).run_once()

    def reconcile_job() -> None:
        """Job mattutino: confronta posizioni attese e reali, senza toccare nulla."""
        costruisci_runner(settings, kill_switch, store).reconcile_now()

    def weekly_job() -> None:
        """Job del lunedi': report di confronto fra live e shadow, sola lettura."""
        report = generate_weekly_report()
        livello = "warning" if report.attenzione else "info"
        build_alerter(settings).send(livello, report.alert_text)
        logger.info("report_settimanale_pronto", path=str(report.path), attenzione=report.attenzione)

    trading_client = TradingClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        paper=settings.alpaca_paper,
    )
    scheduler = build_scheduler(trading_client, trading_job, reconcile_job, weekly_job)
    logger.info("scheduler_avviato", strategia=STRATEGIA, simboli=len(SIMBOLI))
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.warning("scheduler_fermato")


if __name__ == "__main__":
    main()
