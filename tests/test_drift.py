"""`--check`: riscarica senza scrivere e riporta ogni differenza retroattiva."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from sintetici import AlerterFinto, SorgenteFinta, barre_sorgente

from quant.download import parse_args, run_download
from quant.drift import CSV_COLUMNS, DriftResult, compare, run_check
from quant.manifest import sha256_file

STORIA = pd.bdate_range("2018-01-02", "2018-01-31")
FEBBRAIO = date(2018, 2, 1)


@pytest.fixture
def archivio(tmp_path: Path) -> tuple[Path, SorgenteFinta]:
    """SPY e QQQ scaricati dalla sorgente finta, manifest compreso."""
    sorgente = SorgenteFinta({"SPY": barre_sorgente(STORIA), "QQQ": barre_sorgente(STORIA, base=200.0)})
    run_download(
        sorgente, ["SPY", "QQQ"], output_dir=tmp_path / "parquet", report_dir=tmp_path / "r", oggi=FEBBRAIO
    )
    return tmp_path, sorgente


def controlla(cartella: Path, sorgente: SorgenteFinta, **opzioni: Any) -> DriftResult:
    return run_check(
        sorgente,
        ["SPY", "QQQ"],
        output_dir=cartella / "parquet",
        report_dir=cartella / "reports",
        oggi=date(2018, 2, 5),
        **opzioni,
    )


def test_nessuna_deriva_nessun_errore(archivio: tuple[Path, SorgenteFinta]) -> None:
    cartella, sorgente = archivio
    risultato = controlla(cartella, sorgente)

    assert risultato.exit_code == 0 and risultato.checked == ["SPY", "QQQ"]
    assert risultato.csv_path == cartella / "reports" / "data_drift_2018-02-05.csv"
    letto = pd.read_csv(risultato.csv_path)
    assert list(letto.columns) == CSV_COLUMNS and letto.empty


def test_parquet_modificato_a_mano_viene_scoperto(archivio: tuple[Path, SorgenteFinta]) -> None:
    cartella, sorgente = archivio
    parquet = cartella / "parquet" / "SPY.parquet"
    barre = pd.read_parquet(parquet)
    originale = barre.loc["2018-01-10", "close"]
    barre.loc["2018-01-10", "close"] = originale * 1.02
    barre = barre.drop(index=pd.Timestamp("2018-01-17"))
    barre.to_parquet(parquet)
    toccato = parquet.read_bytes()
    manifest = (cartella / "manifest.json").read_bytes()

    alerter = AlerterFinto()
    risultato = controlla(cartella, sorgente, alerter=alerter)

    assert risultato.exit_code == 1
    righe = pd.read_csv(risultato.csv_path)
    spy = righe[righe["symbol"] == "SPY"]
    assert list(zip(spy["date"], spy["kind"], spy["column"].fillna(""), strict=True)) == [
        ("2018-01-10", "changed", "close"),
        ("2018-01-17", "added", ""),
    ]
    cambiata = spy.iloc[0]
    assert cambiata["stored"] == pytest.approx(originale * 1.02) and cambiata["fetched"] == pytest.approx(
        originale
    )
    assert cambiata["relative_change"] == pytest.approx(1 / 1.02 - 1)
    assert cambiata["parquet_sha256"] == sha256_file(parquet)
    assert cambiata["stored_source"] == "finta" and cambiata["check_source"] == "finta"
    assert (righe["symbol"] == "QQQ").sum() == 0

    # riportare non vuol dire correggere: dati e manifest restano come erano
    assert parquet.read_bytes() == toccato and (cartella / "manifest.json").read_bytes() == manifest
    assert len(alerter.messaggi) == 1 and "differenze retroattive su SPY" in alerter.messaggi[0][1]


def test_il_fornitore_riscrive_il_passato(archivio: tuple[Path, SorgenteFinta]) -> None:
    """Il vendor rivede una cedola e toglie una barra: il download incrementale non lo vedrebbe."""
    cartella, sorgente = archivio
    rivista = sorgente.barre["QQQ"].copy()
    rivista.loc["2018-01-12", "dividend"] = 0.35
    rivista.loc["2018-01-22", "volume"] *= 1.5
    sorgente.barre["QQQ"] = rivista.drop(index=pd.Timestamp("2018-01-25"))

    righe = pd.read_csv(controlla(cartella, sorgente).csv_path)
    qqq = righe[righe["symbol"] == "QQQ"]
    assert list(zip(qqq["date"], qqq["kind"], qqq["column"].fillna(""), strict=True)) == [
        ("2018-01-12", "changed", "dividends"),
        ("2018-01-22", "changed", "volume"),
        ("2018-01-25", "removed", ""),
    ]


def test_sorgente_non_disponibile_fa_fallire_il_controllo(archivio: tuple[Path, SorgenteFinta]) -> None:
    cartella, sorgente = archivio
    del sorgente.barre["QQQ"]
    risultato = controlla(cartella, sorgente)
    assert risultato.exit_code == 1 and "QQQ" in risultato.errors


def test_compare_tollera_il_rumore_in_virgola_mobile() -> None:
    barre = barre_sorgente(STORIA)
    salvate = barre.rename(columns={"dividend": "dividends", "split": "split_factor"})
    rumore = barre.copy()
    rumore["close"] *= 1 + 1e-9
    assert compare(salvate, rumore, "SPY").empty
    rumore["close"] *= 1 + 1e-4
    assert len(compare(salvate, rumore, "SPY")) == len(STORIA)


def test_check_e_update_si_escludono() -> None:
    assert parse_args(["--check"]).check is True
    with pytest.raises(SystemExit):
        parse_args(["--check", "--update"])


def test_cli_check_esce_con_uno_sulla_deriva(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from quant import download

    monkeypatch.chdir(tmp_path)
    sorgente = SorgenteFinta({"SPY": barre_sorgente(STORIA)})
    run_download(sorgente, ["SPY"], oggi=FEBBRAIO)
    sorgente.barre["SPY"] = sorgente.barre["SPY"].drop(index=pd.Timestamp("2018-01-17"))
    alerter = AlerterFinto()
    monkeypatch.setattr(download, "crea_sorgente", lambda nome: sorgente)
    monkeypatch.setattr("quant.live.alerts.build_alerter", lambda: alerter)

    with pytest.raises(SystemExit) as uscita:
        download.cli(["--check", "SPY"])
    assert uscita.value.code == 1 and len(alerter.messaggi) == 1
    assert len(list((tmp_path / "reports").glob("data_drift_*.csv"))) == 1
