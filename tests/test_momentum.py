"""Test della strategia momentum su serie sintetiche costruite a mano."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pandas as pd
import pytest

from quant.data import ParquetDataHandler
from quant.events import SignalDirection, SignalEvent
from quant.strategies.momentum import CrossSectionalMomentum

UNIVERSO = ["AAA", "BBB", "CCC", "DDD", "EEE"]
SIMBOLI = [*UNIVERSO, "SPY", "SHY"]


def esegui(directory: Path, **kwargs) -> tuple[list[SignalEvent], CrossSectionalMomentum, ParquetDataHandler]:
    """Percorre tutte le barre e raccoglie i segnali emessi dalla strategia."""
    handler = ParquetDataHandler(directory, SIMBOLI)
    strategia = CrossSectionalMomentum(handler, UNIVERSO, **kwargs)
    segnali: list[SignalEvent] = []
    while handler.continue_backtest:
        for event in handler.update_bars():
            segnali.extend(strategia.on_bar(event))
    return segnali, strategia, handler


def per_data(segnali: list[SignalEvent]) -> dict[pd.Timestamp, list[SignalEvent]]:
    """Segnali raggruppati per giorno di ribilanciamento."""
    gruppi: dict[pd.Timestamp, list[SignalEvent]] = defaultdict(list)
    for segnale in segnali:
        gruppi[pd.Timestamp(segnale.timestamp)].append(segnale)
    return dict(gruppi)


def long_finali(segnali: list[SignalEvent]) -> set[str]:
    """Simboli comprati all'ultimo ribilanciamento."""
    gruppi = per_data(segnali)
    ultimo = gruppi[max(gruppi)]
    return {s.symbol for s in ultimo if s.direction is SignalDirection.LONG}


def test_seleziona_i_due_simboli_in_trend(momentum_parquet_dir: Path) -> None:
    segnali, _, _ = esegui(momentum_parquet_dir, top_n=2)
    assert long_finali(segnali) == {"AAA", "BBB"}


def test_forza_pari_a_uno_su_top_n(momentum_parquet_dir: Path) -> None:
    segnali, _, _ = esegui(momentum_parquet_dir, top_n=2)
    gruppi = per_data(segnali)
    ultimi = gruppi[max(gruppi)]
    assert all(s.strength == 0.5 for s in ultimi if s.direction is SignalDirection.LONG)


def test_ribilanciamento_una_volta_al_mese(momentum_parquet_dir: Path) -> None:
    segnali, _, handler = esegui(momentum_parquet_dir, top_n=2)
    date = sorted(per_data(segnali))
    mesi = {(d.year, d.month) for d in date}
    assert len(date) == len(mesi)
    assert len(date) == len({(t.year, t.month) for t in handler.timeline})


def test_filtro_di_trend_manda_tutto_sul_cash(momentum_bear_parquet_dir: Path) -> None:
    segnali, strategia, _ = esegui(momentum_bear_parquet_dir, top_n=2)
    gruppi = per_data(segnali)
    ultimi = gruppi[max(gruppi)]
    long = [s for s in ultimi if s.direction is SignalDirection.LONG]
    assert [s.symbol for s in long] == ["SHY"]
    assert long[0].strength == 1.0
    assert strategia.desired == {"SHY"}


def test_uscita_dalle_posizioni_non_piu_selezionate(momentum_bear_parquet_dir: Path) -> None:
    segnali, _, _ = esegui(momentum_bear_parquet_dir, top_n=2)
    uscite = {s.symbol for s in segnali if s.direction is SignalDirection.EXIT}
    assert {"AAA", "BBB"} <= uscite
    assert all(s.strength == 0.0 for s in segnali if s.direction is SignalDirection.EXIT)


def test_storico_corto_escluso_dal_ranking(momentum_parquet_dir: Path) -> None:
    """EEE ha il rendimento piu' alto ma solo cento barre: non deve entrare in classifica."""
    segnali, strategia, handler = esegui(momentum_parquet_dir, top_n=2)
    assert all(s.symbol != "EEE" for s in segnali)

    ultimo = pd.Timestamp(handler.timeline[-1])
    assert strategia.momentum("EEE", ultimo) is None
    classifica = dict(strategia.ranking(ultimo))
    assert "EEE" not in classifica
    assert classifica["DDD"] == 0.0
    assert classifica["CCC"] < 0.0


def test_senza_storico_sufficiente_si_resta_sul_cash(momentum_parquet_dir: Path) -> None:
    segnali, _, _ = esegui(momentum_parquet_dir, top_n=2)
    gruppi = per_data(segnali)
    primo = gruppi[min(gruppi)]
    assert [s.symbol for s in primo if s.direction is SignalDirection.LONG] == ["SHY"]


def test_il_mese_piu_recente_e_escluso(momentum_parquet_dir: Path) -> None:
    """Con skip a un mese il rendimento si ferma un mese prima della barra corrente."""
    _, strategia, handler = esegui(momentum_parquet_dir, top_n=2, skip_months=1)
    ultimo = pd.Timestamp(handler.timeline[-1])
    barre = pd.read_parquet(momentum_parquet_dir / "AAA.parquet")
    inizio = barre[barre.index <= ultimo - pd.DateOffset(months=12)]["adj_close"].iloc[-1]
    fine = barre[barre.index <= ultimo - pd.DateOffset(months=1)]["adj_close"].iloc[-1]
    assert strategia.momentum("AAA", ultimo) == pytest.approx(fine / inizio - 1.0)


def test_pesi_fissi_ribilanciano_ogni_mese(momentum_parquet_dir: Path) -> None:
    """Il benchmark statico riemette i pesi a ogni inizio mese, senza mai uscire."""
    from quant.strategies.fixed_weights import FixedWeightsStrategy

    handler = ParquetDataHandler(momentum_parquet_dir, SIMBOLI)
    strategia = FixedWeightsStrategy(handler, {"AAA": 0.6, "BBB": 0.4})
    segnali: list[SignalEvent] = []
    while handler.continue_backtest:
        for event in handler.update_bars():
            segnali.extend(strategia.on_bar(event))

    gruppi = per_data(segnali)
    assert len(gruppi) == len({(t.year, t.month) for t in handler.timeline})
    assert all(len(g) == 2 for g in gruppi.values())
    pesi = {(s.symbol, s.strength) for s in segnali}
    assert pesi == {("AAA", 0.6), ("BBB", 0.4)}
    assert not [s for s in segnali if s.direction is SignalDirection.EXIT]


def test_pesi_fissi_senza_ribilanciamento_entrano_una_volta_sola(momentum_parquet_dir: Path) -> None:
    from quant.strategies.fixed_weights import FixedWeightsStrategy

    handler = ParquetDataHandler(momentum_parquet_dir, SIMBOLI)
    strategia = FixedWeightsStrategy(handler, {"AAA": 1.0}, rebalance_monthly=False)
    segnali: list[SignalEvent] = []
    while handler.continue_backtest:
        for event in handler.update_bars():
            segnali.extend(strategia.on_bar(event))
    assert len(segnali) == 1
