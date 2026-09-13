"""Download vendor-agnostico: schema, aggiornamento incrementale, controlli di qualita' e report."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from sintetici import AlerterFinto, SorgenteFinta, adj_coerente, barre_sorgente

from quant.data import ParquetDataHandler
from quant.download import (
    COLUMNS,
    DownloadResult,
    crea_sorgente,
    parse_args,
    run_download,
    save,
    ultima_data,
)
from quant.sources import DataSource, TiingoSource, YFinanceSource

GENNAIO = pd.bdate_range("2018-01-02", "2018-01-31")
FEBBRAIO = date(2018, 2, 1)


def scarica_in(
    cartella: Path, sorgente: DataSource, simboli: list[str] | None = None, **opzioni: Any
) -> DownloadResult:
    """Download con Parquet e report dentro la cartella del test."""
    return run_download(
        sorgente,
        simboli or ["SPY"],
        output_dir=cartella / "parquet",
        report_dir=cartella / "reports",
        **opzioni,
    )


def leggi(cartella: Path, symbol: str = "SPY") -> pd.DataFrame:
    return pd.read_parquet(cartella / "parquet" / f"{symbol}.parquet")


def test_download_scrive_il_parquet_con_lo_schema_atteso(tmp_path: Path) -> None:
    barre = barre_sorgente(GENNAIO, cedole={"2018-01-10": 0.4}, split={"2018-01-22": 2.0})
    risultato = scarica_in(tmp_path, SorgenteFinta({"SPY": barre}), oggi=FEBBRAIO)

    assert risultato.exit_code == 0
    salvate = leggi(tmp_path)
    assert list(salvate.columns) == list(COLUMNS)
    assert salvate.index.name == "date"
    assert isinstance(salvate.index, pd.DatetimeIndex) and salvate.index.tz is None
    assert (salvate.dtypes == "float64").all()
    assert salvate.index.equals(barre.index)
    assert salvate.loc["2018-01-10", "dividends"] == 0.4
    assert salvate.loc["2018-01-22", "split_factor"] == 2.0
    assert (salvate["dividends"] > 0).sum() == 1 and (salvate["split_factor"] != 1.0).sum() == 1
    pd.testing.assert_series_equal(salvate["close"], barre["close"], check_freq=False)
    pd.testing.assert_series_equal(salvate["adj_close"], barre["adj_close"], check_freq=False)
    assert salvate.loc["2018-01-19", "adj_volume"] == 2 * salvate.loc["2018-01-19", "volume"]
    assert salvate.loc["2018-01-22", "adj_volume"] == salvate.loc["2018-01-22", "volume"]

    handler = ParquetDataHandler(tmp_path / "parquet", ["SPY"])
    assert len(handler.timeline) == len(GENNAIO)


def test_update_aggiunge_solo_le_barre_nuove_senza_riscrivere_le_esistenti(tmp_path: Path) -> None:
    storia = barre_sorgente(pd.bdate_range("2018-01-02", "2018-02-16"))
    sorgente = SorgenteFinta({"SPY": storia})
    scarica_in(tmp_path, sorgente, start="2018-01-01", oggi=FEBBRAIO)
    prima = leggi(tmp_path)
    assert len(prima) == len(GENNAIO)

    # il vendor rivede il passato: l'aggiornamento non deve accorgersene ne' riscriverlo
    rivista = storia.copy()
    rivista.loc[rivista.index < "2018-02-01", ["close", "adj_close"]] *= 1.10
    sorgente.barre["SPY"] = rivista
    risultato = scarica_in(tmp_path, sorgente, update=True, oggi=date(2018, 2, 8))

    assert risultato.outcomes["SPY"].new_bars == 5 and risultato.exit_code == 0
    dopo = leggi(tmp_path)
    assert sorgente.richieste[-1] == ("SPY", date(2018, 2, 1), None)
    pd.testing.assert_frame_equal(dopo.loc[: prima.index[-1]], prima)
    assert list(dopo.index[len(prima) :].strftime("%Y-%m-%d")) == [
        "2018-02-01",
        "2018-02-02",
        "2018-02-05",
        "2018-02-06",
        "2018-02-07",
    ]
    assert list(dopo.columns) == list(COLUMNS)


def test_update_senza_barre_nuove_lascia_il_file_com_era(tmp_path: Path) -> None:
    sorgente = SorgenteFinta({"SPY": barre_sorgente(GENNAIO)})
    scarica_in(tmp_path, sorgente, oggi=FEBBRAIO)
    prima = (tmp_path / "parquet" / "SPY.parquet").read_bytes()

    risultato = scarica_in(tmp_path, sorgente, update=True, oggi=date(2018, 2, 5))
    assert risultato.outcomes["SPY"].new_bars == 0 and not risultato.outcomes["SPY"].written
    assert (tmp_path / "parquet" / "SPY.parquet").read_bytes() == prima


def test_la_seduta_di_oggi_resta_fuori_fino_a_domani(tmp_path: Path) -> None:
    """Una barra presa a mercato aperto e' parziale e l'update non la correggerebbe piu'."""
    sorgente = SorgenteFinta({"SPY": barre_sorgente(GENNAIO)})
    scarica_in(tmp_path, sorgente, oggi=date(2018, 1, 31))
    assert ultima_data("SPY", tmp_path / "parquet") == pd.Timestamp("2018-01-30")

    assert scarica_in(tmp_path, sorgente, update=True, oggi=FEBBRAIO).outcomes["SPY"].new_bars == 1
    assert ultima_data("SPY", tmp_path / "parquet") == pd.Timestamp("2018-01-31")


def test_update_con_cedola_nuova_accoda_e_ricalcola_adj_close(tmp_path: Path) -> None:
    """La cedola nuova sposta adj_close all'indietro, ma i grezzi salvati restano quelli."""
    storia = barre_sorgente(GENNAIO, cedole={"2018-01-30": 1.5})
    sorgente = SorgenteFinta({"SPY": storia})
    scarica_in(tmp_path, sorgente, start="2018-01-01", oggi=date(2018, 1, 29))
    prima = leggi(tmp_path)

    risultato = scarica_in(tmp_path, sorgente, update=True, oggi=FEBBRAIO)
    assert risultato.outcomes["SPY"].new_bars == 3 and risultato.exit_code == 0
    assert [r[1] for r in sorgente.richieste] == [date(2018, 1, 1), date(2018, 1, 27)]

    dopo = leggi(tmp_path)
    grezze = ["open", "high", "low", "close", "volume", "dividends", "split_factor"]
    pd.testing.assert_frame_equal(dopo.loc[: prima.index[-1], grezze], prima[grezze])
    assert list(dopo["adj_close"]) == pytest.approx(list(adj_coerente(storia)), rel=1e-12)
    assert (dopo.loc[: prima.index[-1], "adj_close"] < prima["adj_close"]).all()


def test_adj_close_del_vendor_non_arriva_nel_parquet(tmp_path: Path) -> None:
    """Un vendor che sbaglia di poco adj_close lascia un avviso, il Parquet usa il calcolo in casa."""
    barre = barre_sorgente(GENNAIO, cedole={"2018-01-10": 0.4})
    vendor = barre.assign(adj_close=barre["adj_close"] * ([1.0] * 10 + [1.003] * 12))
    risultato = scarica_in(tmp_path, SorgenteFinta({"SPY": vendor}), oggi=FEBBRAIO)

    report = risultato.outcomes["SPY"].report
    assert report is not None and [f.code for f in report.findings] == ["ADJ_MISMATCH"]
    assert list(leggi(tmp_path)["adj_close"]) == pytest.approx(list(barre["adj_close"]), rel=1e-12)


def test_ultima_data_sul_parquet(spy_parquet_dir: Path, tmp_path: Path) -> None:
    barre = pd.read_parquet(spy_parquet_dir / "SPY.parquet")
    assert ultima_data("SPY", spy_parquet_dir) == barre.index.max()
    assert ultima_data("QQQ", spy_parquet_dir) is None
    assert ultima_data("SPY", tmp_path) is None


def test_blocking_non_lascia_parquet_e_fallisce_con_alert(tmp_path: Path) -> None:
    rotta = barre_sorgente(GENNAIO)
    rotta.loc["2018-01-10", "close"] = -1.0
    alerter = AlerterFinto()
    sorgente = SorgenteFinta({"SPY": barre_sorgente(GENNAIO), "QQQ": rotta})
    risultato = scarica_in(tmp_path, sorgente, ["SPY", "QQQ"], oggi=FEBBRAIO, alerter=alerter)

    assert risultato.exit_code == 1 and risultato.failed == ["QQQ"]
    assert (tmp_path / "parquet" / "SPY.parquet").exists()
    assert sorted(p.name for p in (tmp_path / "parquet").iterdir()) == ["SPY.parquet"]
    assert len(alerter.messaggi) == 1
    livello, testo = alerter.messaggi[0]
    assert livello == "error" and "BLOCKING su QQQ" in testo and "data_quality_2018-02-01.md" in testo
    report = risultato.report_path.read_text()
    assert "| QQQ | bloccato |" in report and "INVALID_PRICE" in report and "non scritto" in report


def test_blocking_in_aggiornamento_lascia_il_file_intatto(tmp_path: Path) -> None:
    storia = barre_sorgente(pd.bdate_range("2018-01-02", "2018-02-16"))
    sorgente = SorgenteFinta({"SPY": storia})
    scarica_in(tmp_path, sorgente, oggi=FEBBRAIO)
    prima = (tmp_path / "parquet" / "SPY.parquet").read_bytes()

    # prima barra nuova con il close quasi raddoppiato e nessuno split a spiegarlo
    rotta = storia.copy()
    rotta.loc["2018-02-01", ["open", "high", "close", "adj_close"]] *= 1.9
    sorgente.barre["SPY"] = rotta
    risultato = scarica_in(tmp_path, sorgente, update=True, oggi=date(2018, 2, 8))

    assert risultato.exit_code == 1
    report = risultato.outcomes["SPY"].report
    assert report is not None
    # il salto all'andata e quello al ritorno, entrambi nelle barre nuove
    assert [(f.code, f.date) for f in report.findings if f.severity == "BLOCKING"] == [
        ("PRICE_JUMP", date(2018, 2, 1)),
        ("PRICE_JUMP", date(2018, 2, 2)),
    ]
    assert (tmp_path / "parquet" / "SPY.parquet").read_bytes() == prima
    assert list((tmp_path / "parquet").iterdir()) == [tmp_path / "parquet" / "SPY.parquet"]


def test_warning_scrive_e_segnala_senza_alert(tmp_path: Path) -> None:
    barre = barre_sorgente(GENNAIO)
    barre.loc["2018-01-10", "volume"] = 0.0
    alerter = AlerterFinto()
    risultato = scarica_in(tmp_path, SorgenteFinta({"SPY": barre}), oggi=FEBBRAIO, alerter=alerter)

    assert risultato.exit_code == 0 and risultato.outcomes["SPY"].written
    assert alerter.messaggi == []
    report = risultato.report_path.read_text()
    assert "| SPY | avvisi |" in report and "ZERO_VOLUME" in report and "2018-01-10" in report


def test_report_scritto_sempre_con_riepilogo_per_simbolo(tmp_path: Path) -> None:
    sorgente = SorgenteFinta({"SPY": barre_sorgente(GENNAIO), "QQQ": barre_sorgente(GENNAIO, base=200.0)})
    risultato = scarica_in(tmp_path, sorgente, ["SPY", "QQQ"], oggi=FEBBRAIO)

    assert risultato.report_path == tmp_path / "reports" / "data_quality_2018-02-01.md"
    report = risultato.report_path.read_text()
    assert report.startswith("# Qualita' dei dati 2018-02-01")
    assert "Sorgente: finta" in report and "nessun blocco" in report
    assert "| SPY | ok | 22 | 2018-01-31 | 0 | 0 | scritto |" in report
    assert "| QQQ | ok | 22 | 2018-01-31 | 0 | 0 | scritto |" in report


def test_unione_dei_simboli_scopre_il_buco_di_uno_solo(tmp_path: Path) -> None:
    """Senza calendario Alpaca il ripiego e' l'unione delle date: basta a vedere un buco isolato."""
    bucata = barre_sorgente(GENNAIO).drop(index=pd.Timestamp("2018-01-17"))
    sorgente = SorgenteFinta({"SPY": barre_sorgente(GENNAIO), "QQQ": bucata})
    risultato = scarica_in(tmp_path, sorgente, ["SPY", "QQQ"], oggi=FEBBRAIO)

    assert risultato.calendar_source == "unione dei simboli"
    findings = risultato.outcomes["QQQ"].report.findings
    assert [(f.code, f.date) for f in findings] == [("MISSING_DAY", date(2018, 1, 17))]
    assert risultato.outcomes["SPY"].report.findings == ()


def test_calendario_del_provider_quando_risponde(tmp_path: Path) -> None:
    sedute = {d.date() for d in GENNAIO}
    risultato = scarica_in(
        tmp_path,
        SorgenteFinta({"SPY": barre_sorgente(GENNAIO)}),
        oggi=FEBBRAIO,
        calendar_provider=lambda inizio, fine: sedute - {date(2018, 1, 15)},
    )
    assert risultato.calendar_source == "alpaca"
    findings = risultato.outcomes["SPY"].report.findings
    assert [(f.code, f.date) for f in findings] == [("EXTRA_DAY", date(2018, 1, 15))]
    assert "Calendario: alpaca" in risultato.report_path.read_text()


def test_sorgente_fuori_contratto_blocca_senza_scrivere(tmp_path: Path) -> None:
    barre = barre_sorgente(GENNAIO).rename(columns={"split": "stock_splits"})
    risultato = scarica_in(tmp_path, SorgenteFinta({"SPY": barre}), oggi=FEBBRAIO)

    assert risultato.exit_code == 1
    assert [f.code for f in risultato.outcomes["SPY"].report.findings] == ["SCHEMA"]
    assert not (tmp_path / "parquet" / "SPY.parquet").exists()


def test_simbolo_opzionale_mancante_non_fa_fallire(tmp_path: Path) -> None:
    sorgente = SorgenteFinta({"SPY": barre_sorgente(GENNAIO)})
    risultato = scarica_in(tmp_path, sorgente, ["BIL", "SPY"], oggi=FEBBRAIO)
    assert risultato.exit_code == 0 and risultato.outcomes["BIL"].status == "saltato"
    assert (tmp_path / "parquet" / "SPY.parquet").exists()
    assert not (tmp_path / "parquet" / "BIL.parquet").exists()

    alerter = AlerterFinto()
    fallito = scarica_in(tmp_path, sorgente, ["QQQ", "SPY"], oggi=FEBBRAIO, alerter=alerter)
    assert fallito.exit_code == 1 and fallito.outcomes["QQQ"].status == "errore"
    assert "errori su QQQ" in alerter.messaggi[0][1]
    assert "QQQ: simbolo sconosciuto" in fallito.report_path.read_text()


def test_nessuna_barra_e_un_errore_non_un_file_vuoto(tmp_path: Path) -> None:
    sorgente = SorgenteFinta({"SPY": barre_sorgente(GENNAIO)})
    risultato = scarica_in(tmp_path, sorgente, start="2018-03-01", oggi=date(2018, 4, 1))
    assert risultato.exit_code == 1 and "nessun dato" in (risultato.outcomes["SPY"].error or "")
    assert not (tmp_path / "parquet" / "SPY.parquet").exists()


def test_scrittura_interrotta_non_tocca_il_file_esistente(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    barre = barre_sorgente(GENNAIO)
    destinazione = save(barre, "SPY", tmp_path)
    prima = destinazione.read_bytes()

    def disco_pieno(self: pd.DataFrame, percorso: Path, *args: Any, **kwargs: Any) -> None:
        Path(percorso).write_bytes(b"PAR1 troncato")
        raise OSError("disco pieno")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", disco_pieno)
    with pytest.raises(OSError, match="disco pieno"):
        save(barre.iloc[:3], "SPY", tmp_path)
    assert destinazione.read_bytes() == prima
    assert [p.name for p in tmp_path.iterdir()] == ["SPY.parquet"]


def test_cli_esce_con_codice_diverso_da_zero_su_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quant import download

    rotta = barre_sorgente(GENNAIO)
    rotta.loc["2018-01-10", "high"] = rotta.loc["2018-01-10", "low"] - 1.0
    alerter = AlerterFinto()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(download, "crea_sorgente", lambda nome: SorgenteFinta({"SPY": rotta}))
    monkeypatch.setattr(download, "alpaca_calendar", lambda: None)
    monkeypatch.setattr("quant.live.alerts.build_alerter", lambda: alerter)

    with pytest.raises(SystemExit) as uscita:
        download.cli(["SPY", "--source", "yfinance"])
    assert uscita.value.code == 1
    assert len(alerter.messaggi) == 1
    assert list((tmp_path / "reports").glob("data_quality_*.md"))
    assert not (tmp_path / "data" / "parquet" / "SPY.parquet").exists()


def test_sorgente_da_riga_di_comando(monkeypatch: pytest.MonkeyPatch) -> None:
    assert parse_args([]).source == "tiingo"
    assert parse_args(["--source", "yfinance", "SPY"]).source == "yfinance"
    assert isinstance(crea_sorgente("yfinance"), YFinanceSource)

    monkeypatch.setenv("TIINGO_API_KEY", "chiave-di-prova")
    assert isinstance(crea_sorgente("tiingo"), TiingoSource)
    with pytest.raises(ValueError, match="sconosciuta"):
        crea_sorgente("bloomberg")


def test_simbolo_ostile_non_scrive_fuori_dalla_cartella(tmp_path: Path) -> None:
    """Un simbolo con `../` uscirebbe da data/parquet: si ferma prima di toccare il disco."""
    sorgente = SorgenteFinta({"SPY": barre_sorgente(GENNAIO)})
    with pytest.raises(ValueError, match="simbolo non valido"):
        scarica_in(tmp_path, sorgente, ["../../fuori"], oggi=FEBBRAIO)
    assert not (tmp_path / "parquet").exists()
    assert not (tmp_path.parent / "fuori.parquet").exists()
