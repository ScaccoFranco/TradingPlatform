"""Esempio completo: buy-and-hold su SPY con costi, dal download alle metriche."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quant.analysis import compute_metrics, format_table
from quant.data import ParquetDataHandler
from quant.engine import Backtest
from quant.execution import SimulatedExecutionHandler
from quant.portfolio import Portfolio
from quant.risk import RiskManager
from quant.strategy import BuyAndHoldStrategy
from scripts.download import fetch, save

SIMBOLO = "SPY"
DATI = Path("data/parquet")
INIZIO = "2010-01-01"
CAPITALE = 100_000.0
COMMISSIONE = 1.0
SLIPPAGE_BPS = 1.0
CUSCINETTO_CASSA = 0.005


def assicura_dati(symbol: str = SIMBOLO, start: str = INIZIO, directory: Path = DATI) -> None:
    """Scarica il Parquet del simbolo solo se non e' gia' presente."""
    if (directory / f"{symbol}.parquet").exists():
        return
    print(f"scarico {symbol}...")
    save(fetch(symbol, start), symbol, directory)


def esegui(credit_dividends: bool) -> tuple[Portfolio, Backtest]:
    """Monta i cinque componenti ed esegue il backtest."""
    data_handler = ParquetDataHandler(DATI, [SIMBOLO], start=INIZIO)
    portfolio = Portfolio(
        data_handler,
        initial_cash=CAPITALE,
        cash_buffer=CUSCINETTO_CASSA,
        credit_dividends=credit_dividends,
    )
    backtest = Backtest(
        data_handler=data_handler,
        strategy=BuyAndHoldStrategy(data_handler, SIMBOLO),
        portfolio=portfolio,
        risk_manager=RiskManager(max_weight_per_symbol=1.0, max_gross_exposure=1.0, max_drawdown=1.0),
        execution_handler=SimulatedExecutionHandler(data_handler, COMMISSIONE, SLIPPAGE_BPS),
    )
    backtest.run()
    return portfolio, backtest


def main() -> None:
    """Confronta il solo rendimento di prezzo con quello che include le cedole."""
    assicura_dati()
    solo_prezzo, backtest = esegui(credit_dividends=False)
    con_cedole, _ = esegui(credit_dividends=True)

    inizio, fine = solo_prezzo.equity_curve[0][0], solo_prezzo.equity_curve[-1][0]
    print(f"{SIMBOLO} buy-and-hold da {inizio.date()} a {fine.date()}, {backtest.market_events} barre")
    print(
        format_table(
            {
                "solo prezzo": compute_metrics(solo_prezzo.equity_curve, solo_prezzo.fills),
                "con cedole": compute_metrics(con_cedole.equity_curve, con_cedole.fills),
            }
        )
    )
    print(f"cedole incassate: {con_cedole.dividends_received:,.0f}")


if __name__ == "__main__":
    main()
