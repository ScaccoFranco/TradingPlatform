"""Shadow backtest: la stessa strategia rigiocata sui dati storici definitivi."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quant.config import PERCORSO_DATI, BacktestConfig
from quant.events import FillEvent
from quant.logging import get_logger
from quant.strategy import Strategy
from quant.validation import run_backtest

logger = get_logger("shadow")

CAPITALE = 100_000.0
COMMISSIONE = 1.0
SLIPPAGE_BPS = 5.0
GIORNI_DI_RISCALDAMENTO = 400


@dataclass(slots=True)
class ShadowResult:
    """Esito di una rigiocata: equity teorica, eseguiti teorici, metriche."""

    start: date
    end: date
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    fills: list[FillEvent] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)

    def equity_series(self) -> pd.Series:
        """Equity teorica come serie indicizzata per data."""
        if not self.equity_curve:
            return pd.Series(dtype="float64")
        return pd.Series(
            [v for _, v in self.equity_curve],
            index=pd.DatetimeIndex([t for t, _ in self.equity_curve]).normalize(),
            dtype="float64",
        )


class ShadowBacktest:
    """Rigioca il backtest dal giorno di avvio del live fino a oggi.

    Usa i Parquet aggiornati, cioe' i prezzi definitivi, con lo stesso capitale e gli
    stessi parametri di costo dichiarati per il live. Serve come termine di paragone:
    se il live si scosta, lo scarto sta nell'esecuzione, non nel segnale.
    """

    def __init__(
        self,
        strategy_factory: Callable[..., Strategy],
        start_date: date | str,
        symbols: Sequence[str],
        path: str | Path = PERCORSO_DATI,
        initial_cash: float = CAPITALE,
        commission_per_trade: float = COMMISSIONE,
        slippage_bps: float = SLIPPAGE_BPS,
        warmup_days: int = GIORNI_DI_RISCALDAMENTO,
        **engine_kwargs: Any,
    ) -> None:
        self.strategy_factory = strategy_factory
        self.start_date = pd.Timestamp(start_date).date()
        self.symbols = list(symbols)
        self.path = Path(path)
        self.initial_cash = initial_cash
        self.commission_per_trade = commission_per_trade
        self.slippage_bps = slippage_bps
        self.warmup_days = warmup_days
        self.engine_kwargs = engine_kwargs

    def run(self, end: date | str | None = None) -> ShadowResult:
        """Esegue la rigiocata fino alla data indicata, per default fino a oggi."""
        fine = pd.Timestamp(end).date() if end is not None else date.today()
        risultato = run_backtest(
            self.strategy_factory,
            self.symbols,
            start=self.start_date.isoformat(),
            end=fine.isoformat(),
            config=self.config(),
            warmup_start=self._riscaldamento(),
            **self.engine_kwargs,
        )
        ombra = ShadowResult(
            start=self.start_date,
            end=fine,
            equity_curve=list(risultato["equity_curve"]),
            fills=list(risultato["fills"]),
            metrics=dict(risultato["metrics"]),
        )
        logger.info(
            "shadow_eseguito",
            start=str(self.start_date),
            end=str(fine),
            barre=len(ombra.equity_curve),
            fills=len(ombra.fills),
        )
        return ombra

    def config(self) -> BacktestConfig:
        """Parametri con cui lo shadow rigioca: gli stessi dichiarati per il live."""
        return BacktestConfig(
            initial_cash=self.initial_cash,
            commission_per_trade=self.commission_per_trade,
            slippage_bps=self.slippage_bps,
            path=self.path,
        )

    def _riscaldamento(self) -> str | None:
        """Data da cui caricare lo storico perche' la strategia arrivi gia' informata."""
        if self.warmup_days <= 0:
            return None
        inizio: str = (pd.Timestamp(self.start_date) - pd.Timedelta(days=self.warmup_days)).date().isoformat()
        return inizio
