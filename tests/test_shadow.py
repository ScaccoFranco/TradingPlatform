"""Test dello shadow backtest e dell'aggiornamento incrementale dei dati."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from quant.data import ParquetDataHandler
from quant.download import ultima_data, unisci
from quant.engine import Backtest
from quant.execution import SimulatedExecutionHandler
from quant.portfolio import Portfolio
from quant.risk import RiskManager
from quant.shadow import ShadowBacktest
from quant.strategy import BuyAndHoldStrategy

CAPITALE = 100_000.0
COMMISSIONE = 1.0
SLIPPAGE = 5.0


def backtest_diretto(directory: Path) -> Portfolio:
    """Backtest costruito a mano, senza passare dallo shadow."""
    handler = ParquetDataHandler(directory, ["SPY"])
    portfolio = Portfolio(handler, initial_cash=CAPITALE, cash_buffer=0.01, credit_dividends=True)
    Backtest(
        handler,
        BuyAndHoldStrategy(handler, "SPY"),
        portfolio,
        RiskManager(),
        SimulatedExecutionHandler(handler, COMMISSIONE, SLIPPAGE),
    ).run()
    return portfolio


def test_shadow_riproduce_gli_stessi_fill(spy_parquet_dir: Path) -> None:
    """Il paragone deve essere identico al backtest, altrimenti misura se stesso."""
    barre = pd.read_parquet(spy_parquet_dir / "SPY.parquet")
    ombra = ShadowBacktest(
        lambda: BuyAndHoldStrategy(None, "SPY"),
        start_date=barre.index[0].date(),
        symbols=["SPY"],
        path=spy_parquet_dir,
        initial_cash=CAPITALE,
        commission_per_trade=COMMISSIONE,
        slippage_bps=SLIPPAGE,
        warmup_days=0,
    ).run(end=barre.index[-1].date())

    atteso = backtest_diretto(spy_parquet_dir)
    assert [(f.timestamp, f.symbol, f.quantity, f.fill_price) for f in ombra.fills] == [
        (f.timestamp, f.symbol, f.quantity, f.fill_price) for f in atteso.fills
    ]
    assert ombra.equity_curve == atteso.equity_curve
    assert ombra.metrics["equity_finale"] == atteso.equity_curve[-1][1]


def test_shadow_riscaldato_parte_dalla_data_di_avvio(momentum_parquet_dir: Path) -> None:
    """Con riscaldamento la strategia arriva informata ma le metriche partono dal live."""
    from quant.strategies.momentum import CrossSectionalMomentum

    universo = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    ombra = ShadowBacktest(
        lambda: CrossSectionalMomentum(None, universo, top_n=2, cash_symbol="SHY"),
        start_date="2016-06-01",
        symbols=[*universo, "SPY", "SHY"],
        path=momentum_parquet_dir,
        warmup_days=400,
    ).run(end="2016-11-30")

    assert ombra.equity_curve
    assert pd.Timestamp(ombra.equity_curve[0][0]).date() >= ombra.start
    assert all(pd.Timestamp(f.timestamp).date() >= ombra.start for f in ombra.fills)
    assert ombra.equity_curve[0][1] == 100_000.0


def test_serie_di_equity_indicizzata_per_data(spy_parquet_dir: Path) -> None:
    barre = pd.read_parquet(spy_parquet_dir / "SPY.parquet")
    ombra = ShadowBacktest(
        lambda: BuyAndHoldStrategy(None, "SPY"),
        start_date=barre.index[0].date(),
        symbols=["SPY"],
        path=spy_parquet_dir,
        warmup_days=0,
    ).run(end=barre.index[-1].date())

    serie = ombra.equity_series()
    assert len(serie) == len(barre)
    assert serie.index.max() == barre.index.max()


def test_unione_preferisce_le_barre_appena_scaricate() -> None:
    vecchio = pd.DataFrame({"close": [1.0, 2.0]}, index=pd.DatetimeIndex(["2026-01-02", "2026-01-05"]))
    nuovo = pd.DataFrame({"close": [2.5, 3.0]}, index=pd.DatetimeIndex(["2026-01-05", "2026-01-06"]))
    unione = unisci(vecchio, nuovo)

    assert list(unione.index.strftime("%Y-%m-%d")) == ["2026-01-02", "2026-01-05", "2026-01-06"]
    assert unione.loc["2026-01-05", "close"] == 2.5
    assert unione.index.name == "date"


def test_ultima_data_sul_parquet(spy_parquet_dir: Path, tmp_path: Path) -> None:
    barre = pd.read_parquet(spy_parquet_dir / "SPY.parquet")
    assert ultima_data("SPY", spy_parquet_dir) == barre.index.max()
    assert ultima_data("QQQ", spy_parquet_dir) is None
    assert ultima_data("SPY", tmp_path) is None


def barre_nuove(date: list[str], cedola: float = 0.0, split: float = 1.0) -> pd.DataFrame:
    """Barre come quelle che restituisce il download, con eventuali operazioni sul capitale."""
    indice = pd.DatetimeIndex(date, name="date")
    n = len(indice)
    return pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.0] * n,
            "adj_close": [100.0] * n,
            "volume": [1_000.0] * n,
            "dividends": [0.0] * (n - 1) + [cedola],
            "split_factor": [1.0] * (n - 1) + [split],
        },
        index=indice,
    )


def test_update_accoda_solo_le_barre_successive(tmp_path: Path) -> None:
    from quant.download import aggiorna

    barre_nuove(["2026-09-07", "2026-09-08"]).to_parquet(tmp_path / "SPY.parquet")
    richieste: list[str] = []

    def finto_download(symbol: str, da: str) -> pd.DataFrame:
        richieste.append(da)
        return barre_nuove(["2026-09-08", "2026-09-09", "2026-09-10"])

    assert aggiorna("SPY", tmp_path, scarica=finto_download) == 2
    assert richieste == ["2026-09-08"]
    salvate = pd.read_parquet(tmp_path / "SPY.parquet")
    attese = ["2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10"]
    assert list(salvate.index.strftime("%Y-%m-%d")) == attese


def test_update_con_cedola_nuova_riscarica_tutta_la_storia(tmp_path: Path) -> None:
    """Una cedola cambia adj_close all'indietro: accodare lascerebbe una giunzione falsa."""
    from quant.download import aggiorna, richiede_riscarico

    barre_nuove(["2026-09-07", "2026-09-08"]).to_parquet(tmp_path / "SPY.parquet")
    richieste: list[str] = []

    def finto_download(symbol: str, da: str) -> pd.DataFrame:
        richieste.append(da)
        if da == "2026-09-08":
            return barre_nuove(["2026-09-08", "2026-09-09"], cedola=1.5)
        return barre_nuove(["2026-09-07", "2026-09-08", "2026-09-09"], cedola=1.5)

    assert aggiorna("SPY", tmp_path, scarica=finto_download) == 1
    assert richieste == ["2026-09-08", "2026-09-07"]
    assert richiede_riscarico(barre_nuove(["2026-09-09"], split=2.0))
    assert not richiede_riscarico(barre_nuove(["2026-09-09"]))


def test_update_senza_barre_nuove(tmp_path: Path) -> None:
    from quant.download import aggiorna

    barre_nuove(["2026-09-07", "2026-09-08"]).to_parquet(tmp_path / "SPY.parquet")

    def nessuna_barra(symbol: str, da: str) -> pd.DataFrame:
        raise RuntimeError("nessun dato")

    assert aggiorna("SPY", tmp_path, scarica=nessuna_barra) == 0
    assert len(pd.read_parquet(tmp_path / "SPY.parquet")) == 2


def test_update_corregge_una_barra_parziale(tmp_path: Path) -> None:
    """Una barra salvata a mercato aperto viene sostituita da quella definitiva."""
    from quant.download import aggiorna

    parziale = barre_nuove(["2026-09-07", "2026-09-08"])
    parziale.loc[pd.Timestamp("2026-09-08"), "close"] = 97.0
    parziale.to_parquet(tmp_path / "SPY.parquet")

    def definitive(symbol: str, da: str) -> pd.DataFrame:
        return barre_nuove(["2026-09-08"])

    assert aggiorna("SPY", tmp_path, scarica=definitive) == 0
    salvate = pd.read_parquet(tmp_path / "SPY.parquet")
    assert salvate.loc[pd.Timestamp("2026-09-08"), "close"] == 100.0
    assert len(salvate) == 2
