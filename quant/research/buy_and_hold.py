"""Esempio minimo: buy-and-hold su SPY con costi, prezzo secco contro cedole incassate."""

from __future__ import annotations

from pathlib import Path

from quant.analysis import compute_metrics, format_table
from quant.config import PERCORSO_DATI, BacktestConfig
from quant.research.common import quiet_logging
from quant.strategy import BuyAndHoldStrategy
from quant.validation import run_backtest

SIMBOLO = "SPY"
INIZIO = "2010-01-01"
CONFIG = BacktestConfig(commission_per_trade=1.0, slippage_bps=1.0, cash_buffer=0.005, path=PERCORSO_DATI)


def controlla_dati(config: BacktestConfig = CONFIG, symbol: str = SIMBOLO) -> None:
    """Errore esplicito se i Parquet non ci sono: il download e' un comando a parte."""
    file = Path(config.path) / f"{symbol}.parquet"
    if not file.exists():
        raise SystemExit(f"dati mancanti in {file}: eseguire prima 'python scripts/download.py'")


def main() -> None:
    """Confronta il solo rendimento di prezzo con quello che include le cedole."""
    quiet_logging()
    controlla_dati()
    risultati = {
        "solo prezzo": run_backtest(
            lambda: BuyAndHoldStrategy(None, SIMBOLO),
            [SIMBOLO],
            INIZIO,
            config=CONFIG.with_overrides(credit_dividends=False, risk_free_symbol=None),
        ),
        "con cedole": run_backtest(
            lambda: BuyAndHoldStrategy(None, SIMBOLO),
            [SIMBOLO],
            INIZIO,
            config=CONFIG.with_overrides(credit_dividends=True, risk_free_symbol=None),
        ),
    }
    equity = risultati["solo prezzo"]["equity_curve"]
    print(f"{SIMBOLO} buy-and-hold da {equity[0][0].date()} a {equity[-1][0].date()}, {len(equity)} barre")
    metriche = {nome: compute_metrics(r["equity_curve"], r["fills"]) for nome, r in risultati.items()}
    print(format_table(metriche))
    print(f"cedole incassate: {risultati['con cedole']['portfolio'].dividends_received:,.0f}")


if __name__ == "__main__":
    main()
