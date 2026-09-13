"""Manifest dei dati: aggiornato da ogni download, custode dell'aggiornamento incrementale."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
from sintetici import SorgenteFinta, barre_sorgente

from quant.download import run_download
from quant.manifest import ManifestEntry, load_manifest, manifest_path, save_manifest, sha256_file

GENNAIO = pd.bdate_range("2018-01-02", "2018-01-31")
STORIA = pd.bdate_range("2018-01-02", "2018-02-16")
FEBBRAIO = date(2018, 2, 1)


def scarica(cartella: Path, sorgente: SorgenteFinta, oggi: date = FEBBRAIO, **opzioni: Any) -> int:
    risultato = run_download(
        sorgente,
        list(sorgente.barre),
        output_dir=cartella / "parquet",
        report_dir=cartella / "reports",
        oggi=oggi,
        **opzioni,
    )
    return risultato.exit_code


def test_il_download_scrive_il_manifest(tmp_path: Path) -> None:
    sorgente = SorgenteFinta({"SPY": barre_sorgente(STORIA), "QQQ": barre_sorgente(STORIA, base=200.0)})
    assert scarica(tmp_path, sorgente) == 0

    percorso = tmp_path / "manifest.json"
    assert percorso == manifest_path(tmp_path / "parquet")
    voci = load_manifest(percorso)
    assert sorted(voci) == ["QQQ", "SPY"]
    assert voci["SPY"] == ManifestEntry(
        source="finta",
        downloaded_at="2018-02-01",
        first="2018-01-02",
        last="2018-01-31",
        bars=len(GENNAIO),
        sha256=sha256_file(tmp_path / "parquet" / "SPY.parquet"),
    )
    grezzo = json.loads(percorso.read_text())
    assert grezzo["version"] == 1 and list(grezzo["symbols"]) == ["QQQ", "SPY"]
    assert "Manifest dopo il download: `" in (tmp_path / "reports" / "data_quality_2018-02-01.md").read_text()


def test_l_aggiornamento_rinnova_la_voce(tmp_path: Path) -> None:
    sorgente = SorgenteFinta({"SPY": barre_sorgente(STORIA)})
    scarica(tmp_path, sorgente)
    prima = load_manifest(tmp_path / "manifest.json")["SPY"]

    assert scarica(tmp_path, sorgente, oggi=date(2018, 2, 8), update=True) == 0
    dopo = load_manifest(tmp_path / "manifest.json")["SPY"]
    assert dopo.last == "2018-02-07" and dopo.bars == prima.bars + 5
    assert dopo.downloaded_at == "2018-02-08" and dopo.sha256 != prima.sha256
    assert dopo.sha256 == sha256_file(tmp_path / "parquet" / "SPY.parquet")


def test_aggiornamento_rifiutato_da_un_altra_sorgente(tmp_path: Path) -> None:
    scarica(tmp_path, SorgenteFinta({"SPY": barre_sorgente(STORIA)}))
    altra = SorgenteFinta({"SPY": barre_sorgente(STORIA)})
    altra.name = "altra"
    prima = (tmp_path / "parquet" / "SPY.parquet").read_bytes()

    assert scarica(tmp_path, altra, oggi=date(2018, 2, 8), update=True) == 1
    assert (tmp_path / "parquet" / "SPY.parquet").read_bytes() == prima
    assert (
        "scaricato da finta, aggiornamento da altra"
        in (tmp_path / "reports" / "data_quality_2018-02-08.md").read_text()
    )


def test_aggiornamento_rifiutato_su_parquet_toccato_a_mano(tmp_path: Path) -> None:
    sorgente = SorgenteFinta({"SPY": barre_sorgente(STORIA)})
    scarica(tmp_path, sorgente)
    parquet = tmp_path / "parquet" / "SPY.parquet"
    barre = pd.read_parquet(parquet)
    barre.iloc[3, barre.columns.get_loc("close")] *= 1.01
    barre.to_parquet(parquet)
    toccato = parquet.read_bytes()

    assert scarica(tmp_path, sorgente, oggi=date(2018, 2, 8), update=True) == 1
    assert parquet.read_bytes() == toccato
    assert (
        "modificato fuori dal download" in (tmp_path / "reports" / "data_quality_2018-02-08.md").read_text()
    )


def test_aggiornamento_rifiutato_senza_voce_nel_manifest(tmp_path: Path) -> None:
    sorgente = SorgenteFinta({"SPY": barre_sorgente(STORIA)})
    scarica(tmp_path, sorgente)
    (tmp_path / "manifest.json").unlink()

    assert scarica(tmp_path, sorgente, oggi=date(2018, 2, 8), update=True) == 1
    assert "assente dal manifest" in (tmp_path / "reports" / "data_quality_2018-02-08.md").read_text()


def test_un_blocking_non_tocca_la_voce_del_manifest(tmp_path: Path) -> None:
    sorgente = SorgenteFinta({"SPY": barre_sorgente(STORIA)})
    scarica(tmp_path, sorgente)
    prima = (tmp_path / "manifest.json").read_bytes()

    rotta = barre_sorgente(STORIA)
    rotta.loc["2018-02-05", "close"] = -1.0
    sorgente.barre["SPY"] = rotta
    assert scarica(tmp_path, sorgente, oggi=date(2018, 2, 8), update=True) == 1
    assert (tmp_path / "manifest.json").read_bytes() == prima


def test_scrittura_ordinata_e_rilettura(tmp_path: Path) -> None:
    voci = {
        "ZZZ": ManifestEntry("tiingo", "2018-02-01", "2018-01-02", "2018-01-31", 21, "a" * 64),
        "AAA": ManifestEntry("tiingo", "2018-02-01", "2018-01-02", "2018-01-31", 21, "b" * 64),
    }
    percorso = save_manifest(voci, tmp_path / "manifest.json")
    assert load_manifest(percorso) == voci
    assert percorso.read_text().index('"AAA"') < percorso.read_text().index('"ZZZ"')
    assert load_manifest(tmp_path / "assente.json") == {}
