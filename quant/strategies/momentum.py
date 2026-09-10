"""Momentum cross-sectional su ETF, ribilanciato alla prima barra di ogni mese."""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from quant.calendar import is_first_trading_day_of_month
from quant.data import DataHandler
from quant.events import MarketEvent, SignalDirection, SignalEvent
from quant.strategy import Strategy

BARRE_PER_MESE = 23  # limite superiore ai giorni di contrattazione in un mese
BARRE_DI_MARGINE = 25  # tolleranza per festivita' e mesi lunghi


class CrossSectionalMomentum(Strategy):
    """Compra i `top_n` ETF con il momentum piu' alto, con filtro di trend sul mercato.

    Il momentum e' il rendimento su `adj_close` fra `lookback_months` fa e
    `skip_months` fa: il mese piu' recente resta escluso, come nella letteratura sul
    reversal di breve. Anche il filtro di trend usa `adj_close`, perche' e' un segnale
    e la regola di dominio vuole i segnali sui prezzi aggiustati.
    """

    def __init__(
        self,
        data_handler: DataHandler,
        symbols: list[str],
        lookback_months: int = 12,
        skip_months: int = 1,
        top_n: int = 3,
        trend_filter_symbol: str | None = "SPY",
        trend_sma_days: int = 200,
        cash_symbol: str = "SHY",
    ) -> None:
        super().__init__(data_handler)
        self.cash_symbol = cash_symbol
        self.symbols = [s for s in symbols if s != cash_symbol]
        self.lookback_months = lookback_months
        self.skip_months = skip_months
        self.top_n = top_n
        self.trend_filter_symbol = trend_filter_symbol
        self.trend_sma_days = trend_sma_days
        self.desired: set[str] = set()
        self.window_bars = lookback_months * BARRE_PER_MESE + BARRE_DI_MARGINE

    def on_bar(self, event: MarketEvent) -> list[SignalEvent]:
        """Ribilancia solo alla prima barra del mese: esce da cio' che non serve, poi entra."""
        if not is_first_trading_day_of_month(event.timestamp, self.data_handler):
            return []

        pesi = self.target_weights(event.timestamp)
        segnali = [
            SignalEvent(event.timestamp, symbol, SignalDirection.EXIT, 0.0)
            for symbol in sorted(self.desired - set(pesi))
        ]
        segnali += [
            SignalEvent(event.timestamp, symbol, SignalDirection.LONG, peso)
            for symbol, peso in sorted(pesi.items())
        ]
        self.desired = set(pesi)
        return segnali

    def target_weights(self, timestamp: datetime) -> dict[str, float]:
        """Allocazione desiderata: i vincitori a forza 1/top_n, oppure tutto sul cash."""
        if self.risk_off(timestamp):
            return {self.cash_symbol: 1.0}

        classifica = self.ranking(timestamp)
        if not classifica:
            return {self.cash_symbol: 1.0}

        forza = 1.0 / self.top_n
        return {symbol: forza for symbol, _ in classifica[: self.top_n]}

    def ranking(self, timestamp: datetime) -> list[tuple[str, float]]:
        """Simboli con storico sufficiente, ordinati per momentum decrescente."""
        punteggi: list[tuple[str, float]] = []
        for symbol in self.symbols:
            valore = self.momentum(symbol, timestamp)
            if valore is not None:
                punteggi.append((symbol, valore))
        return sorted(punteggi, key=lambda coppia: coppia[1], reverse=True)

    def momentum(self, symbol: str, timestamp: datetime) -> float | None:
        """Rendimento su adj_close fra lookback e skip; None se lo storico non basta.

        Uno storico corto fa uscire il simbolo dalla classifica: trattarlo come
        rendimento zero lo metterebbe artificialmente a meta' graduatoria.
        """
        bars = self.data_handler.get_latest_bars(symbol, self.window_bars)
        if bars.empty:
            return None

        corrente = pd.Timestamp(timestamp)
        inizio = corrente - pd.DateOffset(months=self.lookback_months)
        fine = corrente - pd.DateOffset(months=self.skip_months)
        if bars.index[0] > inizio:
            return None

        prezzo_iniziale = self._ultimo_prezzo_entro(bars, inizio)
        prezzo_finale = self._ultimo_prezzo_entro(bars, fine)
        if prezzo_iniziale is None or prezzo_finale is None or prezzo_iniziale <= 0.0:
            return None
        return prezzo_finale / prezzo_iniziale - 1.0

    def risk_off(self, timestamp: datetime) -> bool:
        """True se il mercato di riferimento sta sotto la sua media mobile.

        Con filtro disattivato o storico insufficiente per la media si resta investiti:
        e' il momentum a decidere, il filtro non si inventa un segnale che non ha.
        """
        if self.trend_filter_symbol is None:
            return False
        bars = self.data_handler.get_latest_bars(self.trend_filter_symbol, self.trend_sma_days)
        if len(bars) < self.trend_sma_days:
            return False
        prezzi = bars["adj_close"]
        return float(prezzi.iloc[-1]) < float(prezzi.mean())

    @staticmethod
    def _ultimo_prezzo_entro(bars: pd.DataFrame, limite: pd.Timestamp) -> float | None:
        """Ultimo adj_close con data non successiva al limite."""
        visibili = bars[bars.index <= limite]
        if visibili.empty:
            return None
        return float(visibili["adj_close"].iloc[-1])
