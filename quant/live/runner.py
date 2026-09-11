"""Esecuzione di una giornata di trading, con le stesse classi del backtest."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import structlog

from quant.data import DataHandler
from quant.events import FillEvent, MarketEvent, OrderEvent, SignalEvent
from quant.logging import get_logger
from quant.portfolio import Portfolio
from quant.risk import KillSwitch, RiskDecision, RiskManager
from quant.state import PositionMismatch, StateStore, reconcile
from quant.strategy import Strategy

logger = get_logger("live.runner")


@dataclass(slots=True)
class RunSummary:
    """Esito di una singola esecuzione, per log, alert e test."""

    run_id: str
    timestamp: Any = None
    signals: list[SignalEvent] = field(default_factory=list)
    decisions: list[RiskDecision] = field(default_factory=list)
    submitted: list[OrderEvent] = field(default_factory=list)
    fills: list[FillEvent] = field(default_factory=list)
    mismatches: list[PositionMismatch] = field(default_factory=list)


class LiveRunner:
    """Collega dati, strategia, portafoglio, rischio ed esecuzione per una sola barra.

    Strategia, Portfolio e RiskManager sono le stesse classi del backtest: qui cambia
    solo chi esegue gli ordini e da dove arrivano le barre.
    """

    def __init__(
        self,
        strategy: Strategy,
        portfolio: Portfolio,
        risk_manager: RiskManager,
        execution_handler: Any,
        data_handler: DataHandler,
        state_store: StateStore,
        kill_switch: KillSwitch | None = None,
        alerter: Any = None,
    ) -> None:
        self.strategy = strategy
        self.portfolio = portfolio
        self.risk_manager = risk_manager
        self.execution_handler = execution_handler
        self.data_handler = data_handler
        self.state_store = state_store
        self.kill_switch = kill_switch or risk_manager.kill_switch
        self.alerter = alerter
        self.strategy.data_handler = data_handler

    def run_once(self) -> RunSummary:
        """Una passata completa: stato, barre, segnali, ordini, persistenza, riconciliazione."""
        run_id = uuid.uuid4().hex[:12]
        structlog.contextvars.bind_contextvars(run_id=run_id)
        sommario = RunSummary(run_id=run_id)
        try:
            logger.info("run_iniziata")
            self._carica_stato()
            evento = self._aggiorna_barre(sommario)
            if evento is not None:
                self._genera_segnali(evento, sommario)
                self._invia_ordini(sommario)
            self._persisti(sommario)
            self._riconcilia(sommario)
            logger.info(
                "run_conclusa",
                signals=len(sommario.signals),
                submitted=len(sommario.submitted),
                fills=len(sommario.fills),
                mismatches=len(sommario.mismatches),
            )
            return sommario
        except Exception as errore:
            self._gestisci_eccezione(errore)
            raise
        finally:
            structlog.contextvars.unbind_contextvars("run_id")

    def _carica_stato(self) -> None:
        """Ricostruisce le posizioni attese dallo store e prende la cassa dal broker."""
        self.portfolio.positions = dict(self.state_store.load_positions())
        cassa = self.execution_handler.get_cash()
        self.portfolio.cash = float(cassa)
        logger.info("stato_caricato", positions=self.portfolio.positions, cash=self.portfolio.cash)

    def _aggiorna_barre(self, sommario: RunSummary) -> MarketEvent | None:
        """Porta il cursore all'ultima barra, applica gli eseguiti arrivati e valorizza."""
        evento = self.data_handler.advance_to_latest()
        if evento is None:
            logger.warning("nessuna_barra_disponibile")
            return None

        for fill in self.execution_handler.on_market(evento):
            self._applica_fill(fill, sommario)
        self.portfolio.cash = float(self.execution_handler.get_cash())
        self.portfolio.on_market(evento)
        sommario.timestamp = evento.timestamp
        logger.info("barre_aggiornate", timestamp=str(evento.timestamp), equity=self.portfolio.total_value())
        return evento

    def _applica_fill(self, fill: FillEvent, sommario: RunSummary) -> None:
        """Aggiorna il portafoglio con un eseguito e ne da' notizia."""
        self.portfolio.on_fill(fill)
        sommario.fills.append(fill)
        self._alert("info", f"fill {fill.symbol} {fill.direction} {fill.quantity} @ {fill.fill_price:.2f}")

    def _genera_segnali(self, evento: MarketEvent, sommario: RunSummary) -> None:
        """Interroga la strategia sull'ultima barra.

        La strategia viene rigiocata su tutto lo storico caricato, quindi il suo stato
        interno si ricostruisce da solo dopo un riavvio: contano solo i segnali emessi
        sull'ultima barra, le decisioni precedenti sono gia' state eseguite.
        """
        sommario.signals = list(self.strategy.on_bar(evento))
        descritti = [(s.symbol, str(s.direction), s.strength) for s in sommario.signals]
        logger.info("segnali_generati", signals=descritti)

    def _invia_ordini(self, sommario: RunSummary) -> None:
        """Segnali in ordini, ordini attraverso il rischio, superstiti all'esecuzione."""
        for segnale in sommario.signals:
            ordine = self.portfolio.on_signal(segnale)
            if ordine is None:
                continue
            decisione = self.risk_manager.decide(ordine, self.portfolio)
            sommario.decisions.append(decisione)
            if decisione.order_final is None:
                self._alert("warning", f"ordine rifiutato {ordine.symbol}: {decisione.reason}")
                continue
            if decisione.reduced:
                self._alert("warning", f"ordine ridotto {ordine.symbol}: {decisione.reason}")
            self.execution_handler.on_order(decisione.order_final)
            sommario.submitted.append(decisione.order_final)
            self._alert(
                "info",
                f"ordine inviato {decisione.order_final.symbol} "
                f"{decisione.order_final.direction} {decisione.order_final.quantity}",
            )

    def _persisti(self, sommario: RunSummary) -> None:
        """Salva posizioni attese ed equity della giornata."""
        self.state_store.save_positions(self.portfolio.positions)
        giorno = sommario.timestamp.date() if sommario.timestamp is not None else date.today()
        self.state_store.save_equity(giorno, self.portfolio.cash, self.portfolio.positions_value())
        logger.info("stato_salvato", day=str(giorno), positions=self.portfolio.positions)

    def reconcile_now(self) -> list[PositionMismatch]:
        """Sola riconciliazione, senza inviare ordini: e' il job del mattino."""
        structlog.contextvars.bind_contextvars(run_id=f"rec-{uuid.uuid4().hex[:8]}")
        try:
            self._carica_stato()
            differenze = reconcile(self.portfolio.positions, self.execution_handler.get_positions())
            for differenza in differenze:
                self._alert(
                    "error",
                    f"RECONCILIATION_MISMATCH {differenza.symbol}: "
                    f"attese {differenza.expected}, reali {differenza.actual}",
                )
            return differenze
        finally:
            structlog.contextvars.unbind_contextvars("run_id")

    def _riconcilia(self, sommario: RunSummary) -> None:
        """Confronta le posizioni attese con quelle del broker, senza correggerle."""
        reali = self.execution_handler.get_positions()
        sommario.mismatches = reconcile(self.portfolio.positions, reali)
        for differenza in sommario.mismatches:
            self._alert(
                "error",
                f"RECONCILIATION_MISMATCH {differenza.symbol}: "
                f"attese {differenza.expected}, reali {differenza.actual}",
            )

    def _gestisci_eccezione(self, errore: Exception) -> None:
        """Logga, attiva il kill switch e non ritenta: il ritentativo lo decide una persona."""
        logger.error("run_fallita", errore=str(errore), exc_info=True)
        if self.kill_switch is not None:
            self.kill_switch.activate(f"eccezione in run_once: {errore!r}")
        self._alert("error", f"run fallita, kill switch attivo: {errore!r}")

    def _alert(self, level: str, message: str) -> None:
        """Inoltra all'alerter se configurato; senza alerter resta solo il log."""
        if self.alerter is not None:
            self.alerter.send(level, message)
