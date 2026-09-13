"""Provenienza dei backtest: stessa impronta per lo stesso backtest, impronta diversa se cambia un input."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from quant.config import BacktestConfig
from quant.manifest import ManifestEntry, manifest_path, save_manifest, sha256_file
from quant.provenance import build_provenance, describe_strategy, git_state
from quant.shadow import ShadowBacktest
from quant.strategy import BuyAndHoldStrategy
from quant.validation import BacktestResult, parameter_sensitivity, run_backtest


def esegui(cartella: Path, strength: float = 1.0, **config: object) -> BacktestResult:
    impostazioni = BacktestConfig(path=cartella, risk_free_symbol=None).with_overrides(**config)
    return run_backtest(
        lambda: BuyAndHoldStrategy(None, "SPY", strength=strength), ["SPY"], config=impostazioni
    )


def test_stesso_backtest_stessa_impronta(spy_parquet_dir: Path) -> None:
    primo, secondo = esegui(spy_parquet_dir), esegui(spy_parquet_dir)
    assert primo["provenance"].hash == secondo["provenance"].hash
    assert primo["provenance"] == secondo["provenance"]
    assert len(primo["provenance"].hash) == 64


def test_l_impronta_cambia_con_dati_parametri_e_config(spy_parquet_dir: Path) -> None:
    base = esegui(spy_parquet_dir)["provenance"].hash
    assert esegui(spy_parquet_dir, strength=0.5)["provenance"].hash != base
    assert esegui(spy_parquet_dir, slippage_bps=7.0)["provenance"].hash != base

    barre = pd.read_parquet(spy_parquet_dir / "SPY.parquet")
    barre.iloc[5, barre.columns.get_loc("close")] += 0.01
    barre.to_parquet(spy_parquet_dir / "SPY.parquet")
    modificato = esegui(spy_parquet_dir)["provenance"]
    assert modificato.hash != base
    assert modificato.data_sha256["SPY"] == sha256_file(spy_parquet_dir / "SPY.parquet")


def test_contenuto_del_blocco(spy_parquet_dir: Path) -> None:
    provenienza = esegui(spy_parquet_dir)["provenance"]
    assert provenienza.config["slippage_bps"] == 5.0 and provenienza.config["path"] == str(spy_parquet_dir)
    assert provenienza.run["symbols"] == ["SPY"]
    assert provenienza.run["strategy"] == {
        "class": "quant.strategy.BuyAndHoldStrategy",
        "params": {"strength": 1.0, "symbol": "SPY"},
    }
    commit, sporco = git_state()
    assert provenienza.git_commit == commit and provenienza.git_dirty == sporco
    assert provenienza.manifest_sha256 is None and provenienza.manifest_mismatch == ()
    json.dumps(provenienza.to_dict())

    testo = provenienza.to_markdown()
    assert testo.startswith("## Provenienza") and provenienza.hash in testo
    assert f"SPY `{provenienza.data_sha256['SPY'][:12]}`" in testo


def test_git_assente_non_blocca(tmp_path: Path) -> None:
    assert git_state(tmp_path) == (None, None)


def test_il_repository_ha_un_commit() -> None:
    commit, sporco = git_state()
    assert commit is not None and len(commit) == 40 and isinstance(sporco, bool)


def test_parquet_diverso_dal_manifest_viene_segnalato(spy_parquet_dir: Path) -> None:
    parquet = spy_parquet_dir / "SPY.parquet"
    voce = ManifestEntry("finta", "2018-02-01", "2020-01-01", "2020-02-11", 30, sha256_file(parquet))
    save_manifest({"SPY": voce}, manifest_path(spy_parquet_dir))
    pulito = esegui(spy_parquet_dir)["provenance"]
    assert pulito.manifest_mismatch == () and pulito.manifest_sha256 is not None

    save_manifest({"SPY": replace(voce, sha256="0" * 64)}, manifest_path(spy_parquet_dir))
    sporco = esegui(spy_parquet_dir)["provenance"]
    assert sporco.manifest_mismatch == ("SPY",)
    assert "Dati fuori dal manifest" in sporco.to_markdown()
    # l'hash del manifest si riporta ma non entra nell'impronta
    assert sporco.hash == pulito.hash and sporco.manifest_sha256 != pulito.manifest_sha256


def test_strategia_descritta_senza_stato_ne_data_handler(spy_parquet_dir: Path) -> None:
    strategia = BuyAndHoldStrategy(object(), "SPY")
    descritta = describe_strategy(strategia)
    assert descritta["params"] == {"strength": 1.0, "symbol": "SPY"}


def test_i_parametri_della_strategia_entrano_nell_impronta(spy_parquet_dir: Path) -> None:
    strategia = BuyAndHoldStrategy(None, "SPY")
    config = BacktestConfig(path=spy_parquet_dir)
    prima = build_provenance(config, ["SPY"], None, None, None, strategia)
    strategia.symbol = "QQQ"
    assert build_provenance(config, ["SPY"], None, None, None, strategia).hash != prima.hash


def test_sensitivita_con_impronta_per_riga(spy_parquet_dir: Path) -> None:
    tabella = parameter_sensitivity(
        lambda p: lambda: BuyAndHoldStrategy(None, "SPY", **p),
        {"strength": [0.5, 1.0]},
        ["SPY"],
        config=BacktestConfig(path=spy_parquet_dir, risk_free_symbol=None),
    )
    assert tabella["provenance"].str.len().eq(64).all()
    assert tabella["provenance"].nunique() == 2


def test_lo_shadow_porta_la_provenienza(spy_parquet_dir: Path) -> None:
    barre = pd.read_parquet(spy_parquet_dir / "SPY.parquet")
    ombra = ShadowBacktest(
        lambda: BuyAndHoldStrategy(None, "SPY"),
        start_date=barre.index[0].date(),
        symbols=["SPY"],
        path=spy_parquet_dir,
        warmup_days=0,
    ).run(end=date(2020, 2, 11))
    assert ombra.provenance is not None
    assert ombra.provenance.run["end"] == "2020-02-11"


@pytest.mark.parametrize("dividends_as_cash", [True, False])
def test_la_contabilita_entra_nell_impronta(spy_parquet_dir: Path, dividends_as_cash: bool) -> None:
    provenienza = esegui(spy_parquet_dir, dividends_as_cash=dividends_as_cash)["provenance"]
    assert provenienza.config["dividends_as_cash"] is dividends_as_cash
