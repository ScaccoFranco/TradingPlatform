"""Esempio minimo: buy-and-hold su SPY con costi, cedole in cassa contro prezzi rettificati."""

from __future__ import annotations

from pathlib import Path

from quant.analysis import compute_metrics, format_table
from quant.config import PERCORSO_DATI, BacktestConfig
from quant.provenance import provenance_markdown
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
    """Confronta le due contabilita' del total return: devono dare quasi la stessa equity."""
    quiet_logging()
    controlla_dati()
    risultati = {
        "cedole in cassa": run_backtest(
            lambda: BuyAndHoldStrategy(None, SIMBOLO),
            [SIMBOLO],
            INIZIO,
            config=CONFIG.with_overrides(dividends_as_cash=True, risk_free_symbol=None),
        ),
        "prezzi rettificati": run_backtest(
            lambda: BuyAndHoldStrategy(None, SIMBOLO),
            [SIMBOLO],
            INIZIO,
            config=CONFIG.with_overrides(dividends_as_cash=False, risk_free_symbol=None),
        ),
    }
    equity = risultati["cedole in cassa"]["equity_curve"]
    print(f"{SIMBOLO} buy-and-hold da {equity[0][0].date()} a {equity[-1][0].date()}, {len(equity)} barre")
    metriche = {nome: compute_metrics(r["equity_curve"], r["fills"]) for nome, r in risultati.items()}
    print(format_table(metriche))
    print(f"cedole incassate: {risultati['cedole in cassa']['portfolio'].dividends_received:,.0f}")
    print()
    print(provenance_markdown(risultati))


if __name__ == "__main__":
    main()
