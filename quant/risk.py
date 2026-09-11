"""Controlli di rischio: ogni ordine passa di qui prima dell'esecuzione."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path

from quant.events import OrderDirection, OrderEvent
from quant.logging import get_logger
from quant.portfolio import Portfolio

PERCORSO_KILL = Path("KILL")
logger = get_logger("risk")


class RiskReason(StrEnum):
    """Motivo di una decisione del gestore del rischio."""

    OK = "OK"
    KILL_SWITCH = "KILL_SWITCH"
    NOT_WHITELISTED = "NOT_WHITELISTED"
    MAX_ORDERS_PER_DAY = "MAX_ORDERS_PER_DAY"
    MAX_NOTIONAL_PER_ORDER = "MAX_NOTIONAL_PER_ORDER"
    MAX_WEIGHT_PER_SYMBOL = "MAX_WEIGHT_PER_SYMBOL"
    MAX_GROSS_EXPOSURE = "MAX_GROSS_EXPOSURE"
    MAX_DRAWDOWN = "MAX_DRAWDOWN"
    NO_PRICE = "NO_PRICE"
    ZERO_QUANTITY = "ZERO_QUANTITY"


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """Esito del filtro su un ordine: originale, versione finale e motivo.

    `order_final` e' None se l'ordine e' stato rifiutato, lo stesso oggetto se e'
    passato intatto, un nuovo ordine se e' stato ridotto. Nessun ordine viene mai
    scartato senza una decisione tracciata.
    """

    order_original: OrderEvent
    order_final: OrderEvent | None
    reason: str

    @property
    def rejected(self) -> bool:
        """True se l'ordine non arriva all'esecuzione."""
        return self.order_final is None

    @property
    def reduced(self) -> bool:
        """True se la quantita' finale e' inferiore a quella richiesta."""
        return self.order_final is not None and self.order_final.quantity < self.order_original.quantity


class KillSwitch:
    """Interruttore di emergenza: se attivo nessun ordine passa.

    E' attivo se il file `KILL` esiste nella root o se e' stato attivato in memoria.
    Il file sopravvive al riavvio del processo, il flag in memoria no.
    """

    def __init__(self, path: str | Path = PERCORSO_KILL, active: bool = False) -> None:
        self.path = Path(path)
        self._active = active
        self._reason = "attivato in memoria" if active else ""

    def is_active(self) -> bool:
        """True se il blocco e' in vigore, per file o per flag."""
        return self._active or self.path.exists()

    def activate(self, reason: str) -> None:
        """Attiva il blocco e ne scrive il motivo su file."""
        self._active = True
        self._reason = reason
        self.path.write_text(f"{reason}\n", encoding="utf-8")
        logger.error("kill_switch_attivato", reason=reason, path=str(self.path))

    def deactivate(self) -> None:
        """Rimuove il blocco, file compreso."""
        self._active = False
        self._reason = ""
        self.path.unlink(missing_ok=True)
        logger.warning("kill_switch_disattivato", path=str(self.path))

    def reason(self) -> str:
        """Motivo dell'attivazione, letto dal file se serve."""
        if self._reason:
            return self._reason
        if self.path.exists():
            return self.path.read_text(encoding="utf-8").strip() or "file KILL presente"
        return ""


class RiskManager:
    """Applica tutti i limiti a ogni ordine e restituisce una decisione tracciata.

    E' la stessa classe nel backtest e in live: cambia solo la configurazione passata
    al costruttore. Le regole di sicurezza operativa, cioe' kill switch, whitelist,
    numero di ordini e taglio massimo, valgono per qualunque ordine; drawdown ed
    esposizione lorda invece non bloccano mai chi sta riducendo una posizione.
    """

    def __init__(
        self,
        max_weight_per_symbol: float = 1.0,
        max_gross_exposure: float = 1.0,
        max_drawdown: float = 1.0,
        max_orders_per_day: int | None = None,
        max_notional_per_order: float | None = None,
        allowed_symbols: Iterable[str] | None = None,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        self.max_weight_per_symbol = float(max_weight_per_symbol)
        self.max_gross_exposure = float(max_gross_exposure)
        self.max_drawdown = float(max_drawdown)
        self.max_orders_per_day = max_orders_per_day
        self.max_notional_per_order = max_notional_per_order
        self.allowed_symbols = set(allowed_symbols) if allowed_symbols is not None else None
        self.kill_switch = kill_switch
        self.decisions: list[RiskDecision] = []
        self._orders_per_day: dict[date, int] = {}

    def filter(self, order: OrderEvent, portfolio: Portfolio) -> OrderEvent | None:
        """Interfaccia usata dal motore: restituisce l'ordine finale o None."""
        return self.decide(order, portfolio).order_final

    def decide(self, order: OrderEvent, portfolio: Portfolio) -> RiskDecision:
        """Applica i limiti in ordine e registra la decisione."""
        motivi: list[str] = []
        finale = self._applica(order, portfolio, motivi)
        decisione = RiskDecision(order, finale, "+".join(motivi) if motivi else RiskReason.OK)
        self._registra(decisione)
        return decisione

    def _applica(self, order: OrderEvent, portfolio: Portfolio, motivi: list[str]) -> OrderEvent | None:
        """Catena dei controlli: restituisce l'ordine finale, o None se rifiutato."""
        if self.kill_switch is not None and self.kill_switch.is_active():
            motivi.append(RiskReason.KILL_SWITCH)
            return None
        if self.allowed_symbols is not None and order.symbol not in self.allowed_symbols:
            motivi.append(RiskReason.NOT_WHITELISTED)
            return None
        if self._quota_giornaliera_esaurita(order):
            motivi.append(RiskReason.MAX_ORDERS_PER_DAY)
            return None

        price = portfolio.last_price(order.symbol)
        equity = portfolio.total_value()
        if price is None or price <= 0.0 or equity <= 0.0:
            motivi.append(RiskReason.NO_PRICE)
            return None

        corrente = portfolio.positions.get(order.symbol, 0)
        richiesto = order.quantity if order.direction is OrderDirection.BUY else -order.quantity
        target = corrente + richiesto

        if abs(target) > abs(corrente):
            if self.current_drawdown(portfolio) > self.max_drawdown:
                motivi.append(RiskReason.MAX_DRAWDOWN)
                return None
            target = self._cap_weight(target, price, equity, motivi)
            lorda = self._gross_exposure_after(portfolio, order.symbol, target, price, equity)
            if lorda > self.max_gross_exposure:
                motivi.append(RiskReason.MAX_GROSS_EXPOSURE)
                return None

        delta = self._cap_notional(target - corrente, price, motivi)
        if delta == 0 or (delta > 0) != (richiesto > 0):
            motivi.append(RiskReason.ZERO_QUANTITY)
            return None

        self._conta_ordine(order)
        if delta == richiesto:
            return order
        return OrderEvent(
            timestamp=order.timestamp,
            symbol=order.symbol,
            direction=OrderDirection.BUY if delta > 0 else OrderDirection.SELL,
            quantity=abs(delta),
        )

    def _registra(self, decisione: RiskDecision) -> None:
        """Tiene la decisione in memoria e la logga con il livello adatto."""
        self.decisions.append(decisione)
        dati = {
            "symbol": decisione.order_original.symbol,
            "quantity_original": decisione.order_original.quantity,
            "quantity_final": decisione.order_final.quantity if decisione.order_final else 0,
            "reason": decisione.reason,
        }
        if decisione.rejected:
            logger.warning("ordine_rifiutato", **dati)
        elif decisione.reduced:
            logger.info("ordine_ridotto", **dati)
        else:
            logger.debug("ordine_approvato", **dati)

    def _quota_giornaliera_esaurita(self, order: OrderEvent) -> bool:
        """True se il numero massimo di ordini per quella giornata e' gia' stato raggiunto."""
        if self.max_orders_per_day is None:
            return False
        return self._orders_per_day.get(order.timestamp.date(), 0) >= self.max_orders_per_day

    def _conta_ordine(self, order: OrderEvent) -> None:
        """Incrementa il contatore giornaliero: contano solo gli ordini che passano."""
        if self.max_orders_per_day is None:
            return
        giorno = order.timestamp.date()
        self._orders_per_day[giorno] = self._orders_per_day.get(giorno, 0) + 1

    def orders_today(self, giorno: date) -> int:
        """Ordini approvati in una data giornata."""
        return self._orders_per_day.get(giorno, 0)

    def current_drawdown(self, portfolio: Portfolio) -> float:
        """Drawdown corrente in frazione del picco, letto dal massimo che il portafoglio tiene.

        Il picco e' aggiornato barra per barra dal Portfolio, quindi qui non serve
        piu' riscorrere tutta l'equity curve a ogni ordine.
        """
        if not portfolio.equity_curve:
            return 0.0
        picco = max(portfolio.peak_equity, portfolio.equity_curve[-1][1])
        corrente = portfolio.equity_curve[-1][1]
        if picco <= 0.0:
            return 0.0
        return max(0.0, (picco - corrente) / picco)

    def _cap_weight(self, target: int, price: float, equity: float, motivi: list[str]) -> int:
        """Tronca la quantita' bersaglio al peso massimo per simbolo."""
        massimo = int(self.max_weight_per_symbol * equity // price)
        if abs(target) <= massimo:
            return target
        motivi.append(RiskReason.MAX_WEIGHT_PER_SYMBOL)
        return massimo if target > 0 else -massimo

    def _cap_notional(self, delta: int, price: float, motivi: list[str]) -> int:
        """Tronca la dimensione dell'ordine al controvalore massimo consentito."""
        if self.max_notional_per_order is None or delta == 0:
            return delta
        massimo = int(self.max_notional_per_order // price)
        if abs(delta) <= massimo:
            return delta
        motivi.append(RiskReason.MAX_NOTIONAL_PER_ORDER)
        return massimo if delta > 0 else -massimo

    def _gross_exposure_after(
        self,
        portfolio: Portfolio,
        symbol: str,
        target: int,
        price: float,
        equity: float,
    ) -> float:
        """Esposizione lorda che si avrebbe portando `symbol` a `target`.

        Gli altri simboli sono presi dalla proiezione post-ordini della barra: le
        vendite gia' decise liberano esposizione allo stesso open degli acquisti.
        """
        gross = abs(target * price)
        for altro, quantita in portfolio.projected_positions().items():
            if altro == symbol or quantita == 0:
                continue
            prezzo_altro = portfolio.last_price(altro)
            if prezzo_altro is not None:
                gross += abs(quantita * prezzo_altro)
        return gross / equity
