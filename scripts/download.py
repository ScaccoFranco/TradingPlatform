"""Scarica barre daily da yfinance e le salva in Parquet, un file per simbolo."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

RISK_SYMBOLS = ["SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "TLT", "IEF", "LQD", "HYG", "GLD", "DBC"]
CASH_SYMBOLS = ["SHY", "BIL"]
SYMBOLS = RISK_SYMBOLS + CASH_SYMBOLS
OPTIONAL = {"BIL"}
START = "2005-01-01"
OUTPUT_DIR = Path("data/parquet")
COLUMNS = ["open", "high", "low", "close", "adj_close", "volume"]


def fetch(symbol: str, start: str, end: str | None = None) -> pd.DataFrame:
    """Scarica le barre non aggiustate piu' l'adjusted close e normalizza le colonne."""
    raw = yf.download(
        symbol,
        start=start,
        end=end,
        interval="1d",
        auto_adjust=False,
        actions=False,
        progress=False,
        multi_level_index=False,
    )
    if raw is None or raw.empty:
        raise RuntimeError(f"nessun dato scaricato per {symbol}")
    frame = raw.rename(columns={c: str(c).lower().replace(" ", "_") for c in raw.columns})
    missing = [c for c in COLUMNS if c not in frame.columns]
    if missing:
        raise RuntimeError(f"{symbol}: colonne mancanti {missing}")
    frame = frame[COLUMNS].dropna()
    frame.index = pd.DatetimeIndex(frame.index).tz_localize(None)
    frame.index.name = "date"
    return frame.sort_index()


def save(frame: pd.DataFrame, symbol: str, output_dir: Path) -> Path:
    """Scrive il DataFrame in Parquet e restituisce il percorso."""
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"{symbol}.parquet"
    frame.to_parquet(destination)
    return destination


def main(symbols: list[str] | None = None, start: str = START, output_dir: Path = OUTPUT_DIR) -> None:
    """Scarica e salva tutti i simboli richiesti; i simboli opzionali non bloccano il resto."""
    for symbol in symbols or SYMBOLS:
        try:
            frame = fetch(symbol, start)
        except RuntimeError as error:
            if symbol in OPTIONAL:
                print(f"{symbol}: saltato ({error})")
                continue
            raise
        destination = save(frame, symbol, output_dir)
        print(f"{symbol}: {len(frame)} barre {frame.index[0].date()} -> {frame.index[-1].date()} in {destination}")


if __name__ == "__main__":
    main(sys.argv[1:] or None)
