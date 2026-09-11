"""Scarica barre daily da yfinance e le salva in Parquet, un file per simbolo."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import yfinance as yf

from quant.data import COLUMNS as COLONNE_BARRA

RISK_SYMBOLS = ["SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "TLT", "IEF", "LQD", "HYG", "GLD", "DBC"]
CASH_SYMBOLS = ["SHY", "BIL"]
SYMBOLS = RISK_SYMBOLS + CASH_SYMBOLS
OPTIONAL = {"BIL"}
START = "2005-01-01"
OUTPUT_DIR = Path("data/parquet")
COLUMNS = list(COLONNE_BARRA)  # unica fonte: quant.data


def fetch(symbol: str, start: str, end: str | None = None) -> pd.DataFrame:
    """Scarica le barre con dividendi e split e le normalizza in prezzi grezzi."""
    raw = yf.download(
        symbol,
        start=start,
        end=end,
        interval="1d",
        auto_adjust=False,
        actions=True,
        progress=False,
        multi_level_index=False,
    )
    if raw is None or raw.empty:
        raise RuntimeError(f"nessun dato scaricato per {symbol}")
    frame = raw.rename(columns={c: str(c).lower().replace(" ", "_") for c in raw.columns})
    attese = ["open", "high", "low", "close", "adj_close", "volume"]
    missing = [c for c in attese if c not in frame.columns]
    if missing:
        raise RuntimeError(f"{symbol}: colonne mancanti {missing}")
    frame.index = pd.DatetimeIndex(frame.index).tz_localize(None)
    frame.index.name = "date"
    frame = frame.sort_index()
    return smonta_split(frame)


def smonta_split(frame: pd.DataFrame) -> pd.DataFrame:
    """Riporta i prezzi alla scala del giorno in cui furono scambiati davvero.

    yfinance restituisce prezzi gia' rettificati per gli split, quindi una serie
    continua e una posizione costante. Il broker invece consegna prezzi grezzi e
    raddoppia le azioni il giorno dello split. Qui si sceglie il mondo del broker,
    cosi' backtest e live usano la stessa convenzione: prezzi grezzi, quantita' che
    cambia allo split. `adj_close` resta la serie rettificata usata dai segnali.
    """
    frame = frame.copy()
    grezzi = frame.get("stock_splits")
    fattori = pd.Series(1.0, index=frame.index) if grezzi is None else grezzi.fillna(0.0).replace(0.0, 1.0)
    frame["split_factor"] = fattori.astype("float64")
    cedole = frame.get("dividends", pd.Series(0.0, index=frame.index))
    frame["dividends"] = cedole.fillna(0.0).astype("float64")

    # moltiplicatore da applicare a una barra: prodotto degli split successivi a quella data
    successivi = frame["split_factor"][::-1].shift(1, fill_value=1.0).cumprod()[::-1]
    for colonna in ("open", "high", "low", "close"):
        frame[colonna] = frame[colonna] * successivi
    frame["dividends"] = frame["dividends"] * successivi
    frame["volume"] = frame["volume"] / successivi
    return frame[list(COLUMNS)].dropna()


def save(frame: pd.DataFrame, symbol: str, output_dir: Path) -> Path:
    """Scrive il DataFrame in Parquet e restituisce il percorso."""
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"{symbol}.parquet"
    frame.to_parquet(destination)
    return destination


def ultima_data(symbol: str, output_dir: Path) -> pd.Timestamp | None:
    """Ultima barra gia' presente nel Parquet del simbolo, None se il file non c'e'."""
    destinazione = output_dir / f"{symbol}.parquet"
    if not destinazione.exists():
        return None
    esistente = pd.read_parquet(destinazione)
    if esistente.empty:
        return None
    return pd.Timestamp(esistente.index.max())


def unisci(esistente: pd.DataFrame, nuovo: pd.DataFrame) -> pd.DataFrame:
    """Fonde vecchie e nuove barre: in caso di data ripetuta vince quella appena scaricata.

    Le barre recenti possono essere riviste dal fornitore, quindi vale l'ultima versione
    scaricata; le date piu' vecchie restano come sono.
    """
    unione = pd.concat([esistente, nuovo])
    unione = unione[~unione.index.duplicated(keep="last")].sort_index()
    unione.index.name = "date"
    return unione


def richiede_riscarico(nuove: pd.DataFrame) -> bool:
    """True se fra le barre nuove c'e' una cedola o uno split.

    yfinance ricalcola `adj_close` di tutta la storia a ogni operazione sul capitale:
    accodare le barre nuove a quelle vecchie metterebbe in fila due fattori di
    rettifica diversi, e ogni rendimento a cavallo della giunzione sarebbe sbagliato.
    """
    cedole = nuove["dividends"] > 0.0 if "dividends" in nuove.columns else pd.Series(False)
    split = nuove["split_factor"] != 1.0 if "split_factor" in nuove.columns else pd.Series(False)
    return bool(cedole.any() or split.any())


def aggiorna(
    symbol: str,
    output_dir: Path = OUTPUT_DIR,
    start: str = START,
    scarica: Callable[[str, str], pd.DataFrame] = fetch,
) -> int:
    """Accoda le barre successive all'ultima presente e restituisce quante ne ha aggiunte.

    Riscarica anche l'ultima barra gia' salvata: se era stata presa a mercato aperto
    conteneva un prezzo parziale, e senza questo passaggio resterebbe nel file per
    sempre. Se fra le barre nuove c'e' una cedola o uno split, riscarica invece tutta la
    storia dalla prima data presente, perche' la rettifica di `adj_close` e' cambiata.
    """
    ultima = ultima_data(symbol, output_dir)
    if ultima is None:
        frame = scarica(symbol, start)
        save(frame, symbol, output_dir)
        return len(frame)

    esistente = pd.read_parquet(output_dir / f"{symbol}.parquet")
    try:
        scaricate = scarica(symbol, ultima.date().isoformat())
    except RuntimeError:
        return 0
    scaricate = scaricate[scaricate.index >= ultima]
    nuove = scaricate[scaricate.index > ultima]

    if not nuove.empty and richiede_riscarico(nuove):
        completo = scarica(symbol, pd.Timestamp(esistente.index.min()).date().isoformat())
        save(completo, symbol, output_dir)
        return len(completo) - len(esistente)

    if not scaricate.empty:
        save(unisci(esistente, scaricate), symbol, output_dir)
    return len(nuove)


def main(
    symbols: list[str] | None = None,
    start: str = START,
    output_dir: Path = OUTPUT_DIR,
    update: bool = False,
) -> None:
    """Scarica e salva tutti i simboli richiesti; i simboli opzionali non bloccano il resto."""
    for symbol in symbols or SYMBOLS:
        try:
            if update:
                nuove = aggiorna(symbol, output_dir, start)
                ultima = ultima_data(symbol, output_dir)
                quando = ultima.date() if ultima is not None else "assente"
                print(f"{symbol}: {nuove} barre nuove, ultima {quando}")
                continue
            frame = fetch(symbol, start)
        except RuntimeError as error:
            if symbol in OPTIONAL:
                print(f"{symbol}: saltato ({error})")
                continue
            raise
        destination = save(frame, symbol, output_dir)
        periodo = f"{frame.index[0].date()} -> {frame.index[-1].date()}"
        print(f"{symbol}: {len(frame)} barre {periodo} in {destination}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Argomenti da riga di comando."""
    parser = argparse.ArgumentParser(description="Scarica barre daily in Parquet")
    parser.add_argument("symbols", nargs="*", help="simboli, default l'universo completo")
    parser.add_argument("--update", action="store_true", help="scarica solo le barre mancanti")
    parser.add_argument("--start", default=START, help="data di inizio per il download completo")
    return parser.parse_args(argv)


def cli(argv: list[str] | None = None) -> None:
    """Punto di ingresso da riga di comando."""
    argomenti = parse_args(sys.argv[1:] if argv is None else argv)
    main(argomenti.symbols or None, argomenti.start, update=argomenti.update)


if __name__ == "__main__":
    cli()
