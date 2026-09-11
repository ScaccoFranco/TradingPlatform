"""Pianificazione dei job live: orari di borsa espliciti, mai ora locale."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from quant.logging import get_logger

FUSO_BORSA = ZoneInfo("America/New_York")
ORA_TRADING = (15, 45)
ORA_RICONCILIAZIONE = (9, 35)
ORA_REPORT_SETTIMANALE = (8, 0)
GIORNI_FERIALI = "mon-fri"
GIORNO_REPORT = "mon"
logger = get_logger("live.schedule")


def trading_days(client: Any, start: date, end: date) -> set[date]:
    """Giorni di borsa aperti secondo il calendario Alpaca."""
    from alpaca.trading.requests import GetCalendarRequest

    giorni = client.get_calendar(GetCalendarRequest(start=start, end=end))
    risultato: set[date] = set()
    for giorno in giorni:
        valore = getattr(giorno, "date", None)
        if isinstance(valore, datetime):
            risultato.add(valore.date())
        elif isinstance(valore, date):
            risultato.add(valore)
    return risultato


def is_trading_day(client: Any, giorno: date | None = None) -> bool:
    """True se la borsa e' aperta quel giorno.

    Se il calendario non risponde si ripiega sui giorni feriali e si logga:
    meglio saltare una festivita' sconosciuta che fermare tutto per un errore di rete.
    """
    giorno = giorno or datetime.now(FUSO_BORSA).date()
    try:
        return giorno in trading_days(client, giorno, giorno)
    except Exception as errore:  # noqa: BLE001 - il calendario e' un servizio esterno
        logger.warning("calendario_non_disponibile", errore=str(errore), giorno=str(giorno))
        return giorno.weekday() < 5


def solo_se_borsa_aperta(client: Any, funzione: Any) -> Any:
    """Avvolge un job perche' non parta nei giorni di chiusura."""

    def wrapper() -> Any:
        giorno = datetime.now(FUSO_BORSA).date()
        if not is_trading_day(client, giorno):
            logger.info("giorno_di_chiusura_saltato", giorno=str(giorno))
            return None
        return funzione()

    wrapper.__name__ = getattr(funzione, "__name__", "job")
    return wrapper


def trading_trigger(hour: int, minute: int) -> CronTrigger:
    """Trigger nei giorni feriali all'ora di New York, festivita' escluse a runtime."""
    return CronTrigger(day_of_week=GIORNI_FERIALI, hour=hour, minute=minute, timezone=FUSO_BORSA)


def weekly_trigger(hour: int, minute: int) -> CronTrigger:
    """Trigger del lunedi' mattina, sempre sull'ora di New York."""
    return CronTrigger(day_of_week=GIORNO_REPORT, hour=hour, minute=minute, timezone=FUSO_BORSA)


def build_scheduler(
    client: Any,
    run_once: Any,
    reconcile: Any,
    weekly: Any = None,
    scheduler: Any = None,
) -> Any:
    """Registra trading, riconciliazione mattutina e, se fornito, il report settimanale.

    Il report non passa dal controllo sul calendario: e' sola lettura e ha senso anche
    se il lunedi' la borsa e' chiusa.
    """
    pianificatore = scheduler or BlockingScheduler(timezone=FUSO_BORSA)
    pianificatore.add_job(
        solo_se_borsa_aperta(client, run_once),
        trigger=trading_trigger(*ORA_TRADING),
        id="trading",
        name="run_once",
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1,
    )
    pianificatore.add_job(
        solo_se_borsa_aperta(client, reconcile),
        trigger=trading_trigger(*ORA_RICONCILIAZIONE),
        id="riconciliazione",
        name="reconcile",
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1,
    )
    if weekly is not None:
        pianificatore.add_job(
            weekly,
            trigger=weekly_trigger(*ORA_REPORT_SETTIMANALE),
            id="report_settimanale",
            name="weekly_report",
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
    logger.info(
        "scheduler_pronto",
        trading=f"{ORA_TRADING[0]:02d}:{ORA_TRADING[1]:02d} {FUSO_BORSA}",
        riconciliazione=f"{ORA_RICONCILIAZIONE[0]:02d}:{ORA_RICONCILIAZIONE[1]:02d} {FUSO_BORSA}",
        report_settimanale=weekly is not None,
    )
    return pianificatore


def prossime_esecuzioni(trigger: CronTrigger, quante: int = 10, da: datetime | None = None) -> list[datetime]:
    """Prossimi orari di scatto di un trigger, utile per verificarne il comportamento."""
    momento = da or datetime.now(FUSO_BORSA)
    esecuzioni: list[datetime] = []
    for _ in range(quante):
        momento = trigger.get_next_fire_time(momento, momento + timedelta(seconds=1))
        if momento is None:
            break
        esecuzioni.append(momento)
    return esecuzioni
