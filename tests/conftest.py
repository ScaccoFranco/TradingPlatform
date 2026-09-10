"""Fixture condivise: dataset Parquet sintetici, nessuna rete."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

SYMBOLS = ["SPY", "QQQ", "TLT", "GLD"]


def make_frame(dates: pd.DatetimeIndex, base: float) -> pd.DataFrame:
    """Barre deterministiche crescenti, con adj_close scalato rispetto al close."""
    closes = [base + i for i in range(len(dates))]
    frame = pd.DataFrame(
        {
            "open": [c - 0.5 for c in closes],
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "adj_close": [c * 0.9 for c in closes],
            "volume": [1_000_000 + i for i in range(len(dates))],
        },
        index=dates,
    )
    frame.index.name = "date"
    return frame


@pytest.fixture
def parquet_dir(tmp_path: Path) -> Path:
    """Quattro simboli su 20 giorni lavorativi; TLT ha due barre mancanti."""
    dates = pd.bdate_range("2020-01-01", periods=20)
    directory = tmp_path / "parquet"
    directory.mkdir()
    for i, symbol in enumerate(SYMBOLS):
        frame = make_frame(dates, base=100.0 + 10 * i)
        if symbol == "TLT":
            frame = frame.drop(index=[dates[3], dates[7]])
        frame.to_parquet(directory / f"{symbol}.parquet")
    return directory


HOLIDAYS = ["2021-01-01", "2021-02-15", "2021-04-02", "2021-05-31"]
CALENDAR_SYMBOLS = ["SPY", "IEF", "GLD"]


@pytest.fixture
def holiday_calendar() -> pd.DatetimeIndex:
    """Giorni feriali del primo semestre 2021 meno quattro festivita' NYSE.

    2021-01-01 e' il primo feriale di gennaio ed e' festivo; 2021-05-31 e'
    l'ultimo feriale di maggio ed e' festivo. Servono a coprire i due bordi.
    """
    dates = pd.bdate_range("2021-01-01", "2021-06-30")
    return dates.drop(pd.DatetimeIndex(HOLIDAYS))


@pytest.fixture
def calendar_parquet_dir(tmp_path: Path, holiday_calendar: pd.DatetimeIndex) -> Path:
    """Tre simboli sul calendario con festivita'; GLD salta le prime due barre di marzo."""
    directory = tmp_path / "calendario"
    directory.mkdir()
    for i, symbol in enumerate(CALENDAR_SYMBOLS):
        frame = make_frame(holiday_calendar, base=50.0 + 10 * i)
        if symbol == "GLD":
            marzo = holiday_calendar[holiday_calendar.month == 3][:2]
            frame = frame.drop(index=marzo)
        frame.to_parquet(directory / f"{symbol}.parquet")
    return directory


@pytest.fixture
def spy_parquet_dir(tmp_path: Path) -> Path:
    """SPY su 30 barre con apertura sempre sotto il close precedente: nessuna cassa negativa."""
    dates = pd.bdate_range("2020-01-01", periods=30)
    closes = [100.0 * (1.0 + 0.004 * i) for i in range(len(dates))]
    opens = [closes[0] - 1.0] + [c * 0.995 for c in closes[:-1]]
    frame = pd.DataFrame(
        {
            "open": opens,
            "high": [max(o, c) + 1.0 for o, c in zip(opens, closes)],
            "low": [min(o, c) - 1.0 for o, c in zip(opens, closes)],
            "close": closes,
            "adj_close": [c * 0.98 for c in closes],
            "volume": [2_000_000] * len(dates),
        },
        index=dates,
    )
    frame.index.name = "date"
    directory = tmp_path / "spy"
    directory.mkdir()
    frame.to_parquet(directory / "SPY.parquet")
    return directory


@pytest.fixture
def gap_up_parquet_dir(tmp_path: Path) -> Path:
    """SPY che apre sempre in gap del 3% sopra il close precedente: stressa la cassa."""
    dates = pd.bdate_range("2020-01-01", periods=10)
    closes = [100.0 * 1.03**i for i in range(len(dates))]
    opens = [closes[0]] + [c * 1.03 for c in closes[:-1]]
    frame = pd.DataFrame(
        {
            "open": opens,
            "high": [max(o, c) for o, c in zip(opens, closes)],
            "low": [min(o, c) for o, c in zip(opens, closes)],
            "close": closes,
            "adj_close": closes,
            "volume": [1_000_000] * len(dates),
        },
        index=dates,
    )
    frame.index.name = "date"
    directory = tmp_path / "gap"
    directory.mkdir()
    frame.to_parquet(directory / "SPY.parquet")
    return directory


@pytest.fixture
def dividend_parquet_dir(tmp_path: Path) -> Path:
    """SPY a prezzo fisso 100 con una cedola da 1.0 per azione alla sesta barra.

    L'adj_close codifica la cedola nel rapporto fra due barre consecutive:
    adj_t / adj_(t-1) = (close_t + cedola) / close_(t-1).
    """
    dates = pd.bdate_range("2020-01-01", periods=10)
    fattori = [1.0] * len(dates)
    fattori[5] = 1.01
    adj = [100.0]
    for f in fattori[1:]:
        adj.append(adj[-1] * f)
    frame = pd.DataFrame(
        {
            "open": [100.0] * len(dates),
            "high": [100.0] * len(dates),
            "low": [100.0] * len(dates),
            "close": [100.0] * len(dates),
            "adj_close": adj,
            "volume": [1_000_000] * len(dates),
        },
        index=dates,
    )
    frame.index.name = "date"
    directory = tmp_path / "dividendi"
    directory.mkdir()
    frame.to_parquet(directory / "SPY.parquet")
    return directory


def serie_geometrica(dates: pd.DatetimeIndex, iniziale: float, rendimento_giornaliero: float) -> list[float]:
    """Percorso di prezzo deterministico a rendimento costante."""
    return [iniziale * (1.0 + rendimento_giornaliero) ** i for i in range(len(dates))]


def scrivi_serie(directory: Path, symbol: str, dates: pd.DatetimeIndex, closes: list[float]) -> None:
    """Salva un Parquet con adj_close uguale al close: nessuna cedola nei test di momentum."""
    opens = [closes[0]] + closes[:-1]
    frame = pd.DataFrame(
        {
            "open": opens,
            "high": [max(o, c) for o, c in zip(opens, closes)],
            "low": [min(o, c) for o, c in zip(opens, closes)],
            "close": closes,
            "adj_close": closes,
            "volume": [1_000_000] * len(dates),
        },
        index=dates,
    )
    frame.index.name = "date"
    frame.to_parquet(directory / f"{symbol}.parquet")


MOMENTUM_UNIVERSO = ["AAA", "BBB", "CCC", "DDD", "EEE"]
MOMENTUM_SIMBOLI = [*MOMENTUM_UNIVERSO, "SPY", "SHY"]
BARRE_MOMENTUM = 500
BARRE_STORICO_CORTO = 100


def _costruisci_universo(directory: Path, closes_spy: list[float], dates: pd.DatetimeIndex) -> None:
    """Due simboli in trend netto, due deboli, uno con storico troppo corto."""
    scrivi_serie(directory, "AAA", dates, serie_geometrica(dates, 100.0, 0.0008))
    scrivi_serie(directory, "BBB", dates, serie_geometrica(dates, 100.0, 0.0006))
    scrivi_serie(directory, "CCC", dates, serie_geometrica(dates, 100.0, -0.0002))
    scrivi_serie(directory, "DDD", dates, serie_geometrica(dates, 100.0, 0.0))
    scrivi_serie(directory, "SHY", dates, serie_geometrica(dates, 80.0, 0.00005))
    scrivi_serie(directory, "SPY", dates, closes_spy)
    corte = dates[-BARRE_STORICO_CORTO:]
    scrivi_serie(directory, "EEE", corte, serie_geometrica(corte, 10.0, 0.005))


@pytest.fixture
def momentum_parquet_dir(tmp_path: Path) -> Path:
    """Mercato in salita: il filtro di trend resta inattivo."""
    dates = pd.bdate_range("2015-01-01", periods=BARRE_MOMENTUM)
    directory = tmp_path / "momentum"
    directory.mkdir()
    _costruisci_universo(directory, serie_geometrica(dates, 200.0, 0.0003), dates)
    return directory


@pytest.fixture
def momentum_bear_parquet_dir(tmp_path: Path) -> Path:
    """SPY sale per venti mesi e poi crolla: alla fine sta sotto la sua media mobile."""
    dates = pd.bdate_range("2015-01-01", periods=BARRE_MOMENTUM)
    salita = serie_geometrica(dates[:380], 200.0, 0.0003)
    discesa = [salita[-1] * (1.0 - 0.006) ** (i + 1) for i in range(len(dates) - 380)]
    directory = tmp_path / "momentum_bear"
    directory.mkdir()
    _costruisci_universo(directory, salita + discesa, dates)
    return directory


@pytest.fixture
def walkforward_parquet_dir(tmp_path: Path) -> Path:
    """Dieci anni di barre: AAA sale con regolarita', CCC scende, piu' mercato e cash."""
    dates = pd.bdate_range("2005-01-03", periods=2610)
    directory = tmp_path / "walkforward"
    directory.mkdir()
    scrivi_serie(directory, "AAA", dates, serie_geometrica(dates, 100.0, 0.0004))
    scrivi_serie(directory, "CCC", dates, serie_geometrica(dates, 100.0, -0.0002))
    scrivi_serie(directory, "SPY", dates, serie_geometrica(dates, 200.0, 0.0002))
    scrivi_serie(directory, "SHY", dates, serie_geometrica(dates, 80.0, 0.00005))
    return directory
