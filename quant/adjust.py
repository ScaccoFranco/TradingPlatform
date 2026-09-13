"""Corporate actions gestite in casa: l'unico punto in cui si produce `adj_close`.

Metodo standard a fattore cumulativo all'indietro. Alla data ex t la cedola D_t e lo split
s_t cambiano la scala di tutte le barre precedenti del fattore
`(1 - D_t * s_t / close_{t-1}) / s_t`, e il fattore di una barra e' il prodotto di quelli
delle date ex successive. L'ultima barra ha fattore 1, quindi `adj_close` coincide con il
close grezzo di oggi e le barre passate sono espresse nella scala attuale.

`adj_t / adj_{t-1} = close_t / (close_{t-1} / s_t - D_t)`: il rendimento rettificato
reinveste la cedola al prezzo ex, lo stesso metodo di CRSP e dei principali vendor. Una
cedola sulla prima barra non ha un close precedente e resta senza effetto.
"""

from __future__ import annotations

import pandas as pd

PREZZI = ("open", "high", "low", "close")


def step_factors(close: pd.Series, dividend: pd.Series, split: pd.Series) -> pd.Series:
    """Fattore che la barra t applica a tutte le precedenti; NaN sulla prima barra."""
    passo: pd.Series = (1.0 - dividend * split / close.shift(1)) / split
    return passo


def adjustment_factors(close: pd.Series, dividend: pd.Series, split: pd.Series) -> pd.Series:
    """Fattore cumulativo di ogni barra: prodotto dei passi delle date successive, 1 sull'ultima."""
    passi = step_factors(close, dividend, split)
    dalla_barra_in_poi = passi[::-1].cumprod()[::-1]
    fattori: pd.Series = dalla_barra_in_poi.shift(-1, fill_value=1.0)
    return fattori


def split_only_factors(split: pd.Series) -> pd.Series:
    """Prodotto degli split successivi a ogni barra: riporta i volumi alle azioni di oggi."""
    fattori: pd.Series = split[::-1].cumprod()[::-1].shift(-1, fill_value=1.0)
    return fattori


def _colonne_operazioni(frame: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Cedole e split col nome del contratto delle sorgenti o con quello dei Parquet.

    Una colonna assente vale come nessuna operazione, come fa il Portfolio.
    """
    nomi = ("dividend", "split") if "dividend" in frame.columns else ("dividends", "split_factor")
    cedole = frame[nomi[0]] if nomi[0] in frame.columns else pd.Series(0.0, index=frame.index)
    split = frame[nomi[1]] if nomi[1] in frame.columns else pd.Series(1.0, index=frame.index)
    return cedole.fillna(0.0), split.fillna(1.0)


def adjust(df: pd.DataFrame) -> pd.DataFrame:
    """Copia del frame con `adj_close` e `adj_volume` ricalcolati dai grezzi.

    Accetta sia il formato delle sorgenti (`dividend`, `split`) sia quello dei Parquet
    (`dividends`, `split_factor`). Un `adj_close` gia' presente, per esempio quello del
    vendor, viene sostituito: serve solo al confronto nei controlli di qualita'.
    """
    cedole, split = _colonne_operazioni(df)
    rettificato = df.copy()
    rettificato["adj_close"] = df["close"] * adjustment_factors(df["close"], cedole, split)
    rettificato["adj_volume"] = df["volume"] * split_only_factors(split)
    return rettificato


def adjusted_view(df: pd.DataFrame) -> pd.DataFrame:
    """Barre Parquet con open, high, low e close sulla scala rettificata e nessuna operazione.

    E' la contabilita' di `BacktestConfig(dividends_as_cash=False)`: il total return sta tutto
    nel prezzo, quindi cedole e split si azzerano e Portfolio ed esecuzione lavorano come su
    un titolo senza operazioni sul capitale. `adj_close` resta quello salvato, cosi' la
    strategia vede gli stessi segnali nelle due contabilita'. La scala rettificata dipende
    dalle cedole future, quindi questa vista esiste solo nel backtest, mai in live.
    """
    cedole, split = _colonne_operazioni(df)
    fattori = adjustment_factors(df["close"], cedole, split)
    vista = df.copy()
    for colonna in PREZZI:
        vista[colonna] = df[colonna] * fattori
    vista["volume"] = df["volume"] * split_only_factors(split)
    if "dividends" in vista.columns:
        vista["dividends"] = 0.0
    if "split_factor" in vista.columns:
        vista["split_factor"] = 1.0
    return vista
