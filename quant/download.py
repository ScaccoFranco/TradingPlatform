"""Scarica barre daily da una `DataSource`, le controlla e le salva in Parquet, un file per simbolo."""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from quant.adjust import adjust
from quant.config import DataQualityConfig
from quant.data import COLUMNS as COLONNE_BARRA
from quant.data_quality import (
    CalendarProvider,
    DataQualityReport,
    Finding,
    Severity,
    alpaca_calendar,
    build_calendar,
    check,
)
from quant.manifest import (
    ManifestEntry,
    ProvenanceError,
    entry_for,
    load_manifest,
    manifest_path,
    manifest_sha256,
    save_manifest,
    verify,
)
from quant.sources import (
    DataSource,
    DataSourceError,
    SchemaError,
    TiingoSource,
    YFinanceSource,
    valida_simbolo,
    validate_schema,
)

RISK_SYMBOLS = ["SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "TLT", "IEF", "LQD", "HYG", "GLD", "DBC"]
CASH_SYMBOLS = ["SHY", "BIL"]
SYMBOLS = RISK_SYMBOLS + CASH_SYMBOLS
OPTIONAL = {"BIL"}
START = "2005-01-01"
OUTPUT_DIR = Path("data/parquet")
REPORT_DIR = Path("reports")
COLUMNS = [*COLONNE_BARRA, "adj_volume"]  # schema delle barre in quant.data, piu' il volume rettificato
SORGENTI = ("tiingo", "yfinance")
SORGENTE_DEFAULT = "tiingo"
NEW_YORK = ZoneInfo("America/New_York")
RIGHE_PER_SIMBOLO = 20


def crea_sorgente(nome: str) -> DataSource:
    """Sorgente dati dal nome usato in riga di comando."""
    if nome == "tiingo":
        return TiingoSource.from_settings()
    if nome == "yfinance":
        return YFinanceSource()
    raise ValueError(f"sorgente sconosciuta {nome!r}, ammesse: {', '.join(SORGENTI)}")


def in_formato_parquet(frame: pd.DataFrame) -> pd.DataFrame:
    """Dal contratto di `DataSource` allo schema dei Parquet, con `adj_close` calcolato in casa.

    `dividend` e `split` diventano `dividends` e `split_factor`, i nomi che Portfolio ed
    esecuzione leggono gia'. `adj_close` e `adj_volume` escono da `quant.adjust.adjust` sulla
    storia intera: l'eventuale `adj_close` del vendor e' servito solo ai controlli.
    """
    parquet = adjust(frame).rename(columns={"dividend": "dividends", "split": "split_factor"})
    return parquet[COLUMNS]


def da_parquet(frame: pd.DataFrame) -> pd.DataFrame:
    """Dallo schema dei Parquet al contratto, per controllare le barre salvate insieme alle nuove.

    L'`adj_close` salvato e' quello calcolato in casa, su una scala diversa da quella che il
    vendor usa oggi: si toglie, cosi' il confronto col vendor riguarda solo le barre nuove.
    """
    contratto = frame.rename(columns={"dividends": "dividend", "split_factor": "split"})
    return contratto.drop(columns=["adj_close", "adj_volume"], errors="ignore")


def sessioni_chiuse(frame: pd.DataFrame, oggi: date | None = None) -> pd.DataFrame:
    """Solo le barre di sedute gia' chiuse: quella di oggi puo' essere parziale e resta fuori.

    Una barra presa a mercato aperto ha un close provvisorio, e l'aggiornamento non
    riscrive le barre gia' salvate: se entrasse nel Parquet ci resterebbe. Si salva quindi
    fino a ieri, ora di New York, e la seduta di oggi arriva col download di domani.
    """
    limite = oggi or datetime.now(NEW_YORK).date()
    return frame[frame.index < pd.Timestamp(limite)]


def scarica(
    source: DataSource,
    symbol: str,
    start: date | str,
    end: date | str | None = None,
    oggi: date | None = None,
) -> pd.DataFrame:
    """Barre del simbolo dalla sorgente, verificate sul contratto, solo sedute chiuse."""
    grezze = source.fetch(symbol, start, end)
    validate_schema(grezze)
    return sessioni_chiuse(grezze, oggi)


def storia_completa(
    source: DataSource, symbol: str, start: date | str, oggi: date | None = None
) -> pd.DataFrame:
    """Tutte le barre da start in poi; nessuna barra e' un errore, non un file vuoto."""
    frame = scarica(source, symbol, start, oggi=oggi)
    if frame.empty:
        raise DataSourceError(f"nessun dato scaricato per {symbol}")
    return frame


def save(frame: pd.DataFrame, symbol: str, output_dir: Path) -> Path:
    """Scrive il Parquet in un file temporaneo e lo sostituisce solo a scrittura riuscita.

    Un errore a meta' lascia il file precedente com'era, mai uno troncato.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"{symbol}.parquet"
    temporaneo = destination.with_name(f".{destination.name}.tmp")
    try:
        frame.to_parquet(temporaneo)
        os.replace(temporaneo, destination)
    finally:
        temporaneo.unlink(missing_ok=True)
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


@dataclass(frozen=True, slots=True)
class Candidate:
    """Serie di un simbolo pronta per i controlli, nel formato del contratto.

    `since` e' la prima barra nuova in un aggiornamento: le precedenti fanno solo da
    contesto. `write` e' falso quando non c'e' niente da aggiungere.
    """

    symbol: str
    serie: pd.DataFrame
    since: pd.Timestamp | None
    new_bars: int
    write: bool = True


def prepara(
    symbol: str,
    source: DataSource,
    output_dir: Path,
    start: str,
    update: bool,
    oggi: date | None = None,
    registro: dict[str, ManifestEntry] | None = None,
) -> Candidate:
    """Scarica le barre di un simbolo senza scrivere nulla.

    Con `update` i grezzi gia' salvati non si toccano: alla sorgente si chiede solo dal
    giorno dopo l'ultima barra. Una cedola o uno split fra le barre nuove non richiede di
    riscaricare nulla, perche' `adj_close` si ricalcola in casa su tutta la storia. Prima di
    accodare, il Parquet deve essere quello del manifest e venire dalla stessa sorgente.
    """
    ultima = ultima_data(symbol, output_dir) if update else None
    if ultima is None:
        serie = storia_completa(source, symbol, start, oggi)
        return Candidate(symbol, serie, None, len(serie))

    verify(symbol, output_dir / f"{symbol}.parquet", registro or {}, source.name)
    esistente = da_parquet(pd.read_parquet(output_dir / f"{symbol}.parquet"))
    nuove = scarica(source, symbol, (ultima + pd.Timedelta(days=1)).date(), oggi=oggi)
    nuove = nuove[nuove.index > ultima]
    if nuove.empty:
        return Candidate(symbol, esistente, None, 0, write=False)
    serie = pd.concat([esistente, nuove]).sort_index()
    serie.index.name = "date"
    return Candidate(symbol, serie, pd.Timestamp(nuove.index[0]), len(nuove))


@dataclass(slots=True)
class SymbolOutcome:
    """Com'e' andato un simbolo: barre nuove, controlli, errori, scrittura."""

    symbol: str
    optional: bool = False
    new_bars: int = 0
    last: pd.Timestamp | None = None
    report: DataQualityReport | None = None
    error: str | None = None
    written: bool = False

    @property
    def blocked(self) -> bool:
        """True se un finding BLOCKING ha impedito la scrittura."""
        return self.report is not None and self.report.blocking

    @property
    def failed(self) -> bool:
        """True se il simbolo fa fallire il download: bloccato, o errore su un simbolo obbligatorio."""
        return self.blocked or (self.error is not None and not self.optional)

    @property
    def status(self) -> str:
        """Esito in una parola, per il report."""
        if self.error is not None:
            return "saltato" if self.optional else "errore"
        if self.blocked:
            return "bloccato"
        if self.report is not None and self.report.count(Severity.WARNING):
            return "avvisi"
        return "ok"


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Esito di un download: un `SymbolOutcome` per simbolo e il report scritto."""

    outcomes: dict[str, SymbolOutcome]
    calendar_source: str
    report_path: Path

    @property
    def failed(self) -> list[str]:
        """Simboli che rendono il download fallito."""
        return [s for s, esito in self.outcomes.items() if esito.failed]

    @property
    def exit_code(self) -> int:
        """Codice di uscita del processo: diverso da zero con un BLOCKING o un errore di sorgente."""
        return 1 if self.failed else 0


def run_download(
    source: DataSource,
    symbols: list[str] | None = None,
    *,
    start: str = START,
    output_dir: Path = OUTPUT_DIR,
    update: bool = False,
    oggi: date | None = None,
    calendar_provider: CalendarProvider | None = None,
    alerter: Any = None,
    report_dir: Path = REPORT_DIR,
    quality: DataQualityConfig | None = None,
) -> DownloadResult:
    """Scarica tutti i simboli, li controlla e scrive solo quelli senza BLOCKING.

    Prima si scarica tutto, poi si costruisce il calendario, poi si controlla e si scrive:
    il ripiego sull'unione delle date ha bisogno di tutti i simboli. Ogni Parquet scritto
    aggiorna la sua voce in `data/manifest.json`. Il report di qualita' si scrive sempre;
    con un BLOCKING, un errore di sorgente o un Parquet fuori manifest parte l'alert.
    """
    giorno = oggi or datetime.now(NEW_YORK).date()
    soglie = quality or DataQualityConfig()
    manifest = manifest_path(output_dir)
    registro = load_manifest(manifest)
    print(f"sorgente: {source.name}")
    esiti: dict[str, SymbolOutcome] = {}
    candidati: dict[str, Candidate] = {}
    for symbol in [valida_simbolo(s) for s in symbols or SYMBOLS]:
        esito = esiti[symbol] = SymbolOutcome(symbol, optional=symbol in OPTIONAL)
        try:
            candidati[symbol] = prepara(symbol, source, output_dir, start, update, giorno, registro)
        except (DataSourceError, ProvenanceError) as errore:
            esito.error = str(errore)
        except SchemaError as errore:
            finding = Finding(Severity.BLOCKING, "SCHEMA", None, f"fuori contratto: {errore}")
            esito.report = DataQualityReport(symbol, (finding,))

    calendario, origine = build_calendar({s: c.serie for s, c in candidati.items()}, calendar_provider)
    print(f"calendario: {origine}")
    for symbol, candidato in candidati.items():
        esito = esiti[symbol]
        esito.new_bars = candidato.new_bars
        if candidato.write:
            esito.report = check(candidato.serie, symbol, calendario, since=candidato.since, config=soglie)
            if not esito.report.blocking:
                destinazione = save(in_formato_parquet(candidato.serie), symbol, output_dir)
                registro[symbol] = entry_for(destinazione, source.name, giorno)
                esito.written = True
    if any(e.written for e in esiti.values()):
        save_manifest(registro, manifest)

    for symbol, esito in esiti.items():
        esito.last = ultima_data(symbol, output_dir)
        print(_riga_console(esito))

    percorso = scrivi_report(
        esiti, report_dir, giorno, source.name, origine, calendario, update, soglie, manifest_sha256(manifest)
    )
    risultato = DownloadResult(esiti, origine, percorso)
    print(f"report: {percorso}")
    if risultato.failed and alerter is not None:
        alerter.send("error", _testo_alert(risultato, giorno))
    return risultato


def _riga_console(esito: SymbolOutcome) -> str:
    quando = esito.last.date() if esito.last is not None else "assente"
    if esito.error is not None:
        return f"{esito.symbol}: {esito.status} ({esito.error})"
    stato = f"{esito.new_bars} barre nuove, ultima {quando}"
    if esito.report is None:
        return f"{esito.symbol}: {stato}"
    bloccanti = esito.report.count(Severity.BLOCKING)
    avvisi = esito.report.count(Severity.WARNING)
    scritto = "scritto" if esito.written else "NON SCRITTO"
    return f"{esito.symbol}: {stato}, {bloccanti} bloccanti, {avvisi} avvisi, {scritto}"


def _testo_alert(risultato: DownloadResult, giorno: date) -> str:
    bloccati = [s for s, e in risultato.outcomes.items() if e.blocked]
    errori = [s for s, e in risultato.outcomes.items() if e.error is not None and not e.optional]
    parti = []
    if bloccati:
        parti.append(f"BLOCKING su {', '.join(bloccati)}, Parquet non scritti")
    if errori:
        parti.append(f"errori su {', '.join(errori)}")
    return f"download dati {giorno}: {'; '.join(parti)}. Report {risultato.report_path}"


def scrivi_report(
    esiti: dict[str, SymbolOutcome],
    report_dir: Path,
    giorno: date,
    sorgente: str,
    origine_calendario: str,
    calendario: pd.DatetimeIndex,
    update: bool,
    soglie: DataQualityConfig,
    manifest: str | None = None,
) -> Path:
    """Scrive `data_quality_YYYY-MM-DD.md` con il riepilogo per simbolo e i dettagli."""
    report_dir.mkdir(parents=True, exist_ok=True)
    percorso = report_dir / f"data_quality_{giorno.isoformat()}.md"
    percorso.write_text(
        format_report(esiti, giorno, sorgente, origine_calendario, calendario, update, soglie, manifest),
        encoding="utf-8",
    )
    return percorso


def format_report(
    esiti: dict[str, SymbolOutcome],
    giorno: date,
    sorgente: str,
    origine_calendario: str,
    calendario: pd.DatetimeIndex,
    update: bool,
    soglie: DataQualityConfig,
    manifest: str | None = None,
) -> str:
    """Markdown del report di qualita'; in coda l'hash del manifest dopo il download."""
    falliti = [s for s, e in esiti.items() if e.failed]
    periodo = (
        f", {len(calendario)} sedute dal {calendario[0].date()} al {calendario[-1].date()}"
        if len(calendario)
        else ""
    )
    righe = [
        f"# Qualita' dei dati {giorno.isoformat()}",
        "",
        f"- Sorgente: {sorgente}",
        f"- Modalita': {'aggiornamento incrementale' if update else 'download completo'}",
        f"- Calendario: {origine_calendario}{periodo}",
        f"- Soglie: salto di close {soglie.max_close_jump:.0%}, tolleranza su [low, high] "
        f"{soglie.range_tolerance:.2%}, scarto adj_close {soglie.adj_close_warning:.2%} avviso "
        f"e {soglie.adj_close_blocking:.2%} blocco",
        f"- Esito: {'FALLITO su ' + ', '.join(falliti) if falliti else 'nessun blocco'}",
        "",
        "| Simbolo | Esito | Barre nuove | Ultima barra | Bloccanti | Avvisi | Parquet |",
        "|---|---|---:|---|---:|---:|---|",
    ]
    for esito in esiti.values():
        ultima = esito.last.date().isoformat() if esito.last is not None else "-"
        bloccanti = str(esito.report.count(Severity.BLOCKING)) if esito.report else "-"
        avvisi = str(esito.report.count(Severity.WARNING)) if esito.report else "-"
        parquet = "scritto" if esito.written else ("non scritto" if esito.blocked else "invariato")
        righe.append(
            f"| {esito.symbol} | {esito.status} | {esito.new_bars} | {ultima} "
            f"| {bloccanti} | {avvisi} | {parquet} |"
        )

    errori = [e for e in esiti.values() if e.error is not None]
    if errori:
        righe += ["", "## Errori", ""]
        righe += [f"- {e.symbol}{' (opzionale)' if e.optional else ''}: {e.error}" for e in errori]

    con_finding = [e for e in esiti.values() if e.report is not None and e.report.findings]
    if con_finding:
        righe += ["", "## Dettaglio"]
    for esito in con_finding:
        assert esito.report is not None
        conteggio = Counter((f.severity.value, f.code) for f in esito.report.findings)
        ordinati = sorted(
            esito.report.findings,
            key=lambda f: (f.severity is not Severity.BLOCKING, f.date or date.min),
        )
        righe += [
            "",
            f"### {esito.symbol}",
            "",
            "Conteggio: " + ", ".join(f"{s} {c} {n}" for (s, c), n in sorted(conteggio.items())),
            "",
            "| Severita' | Codice | Data | Messaggio |",
            "|---|---|---|---|",
        ]
        for f in ordinati[:RIGHE_PER_SIMBOLO]:
            quando = f.date.isoformat() if f.date is not None else "-"
            righe.append(f"| {f.severity.value} | {f.code} | {quando} | {f.message.replace('|', '/')} |")
        if len(ordinati) > RIGHE_PER_SIMBOLO:
            righe.append(f"\n... e altri {len(ordinati) - RIGHE_PER_SIMBOLO} finding, vedi il conteggio.")
    righe += ["", "## Provenienza", "", f"- Manifest dopo il download: `{manifest or 'assente'}`"]
    return "\n".join(righe) + "\n"


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Argomenti da riga di comando."""
    parser = argparse.ArgumentParser(description="Scarica barre daily in Parquet")
    parser.add_argument("symbols", nargs="*", help="simboli, default l'universo completo")
    modo = parser.add_mutually_exclusive_group()
    modo.add_argument("--update", action="store_true", help="scarica solo le barre mancanti")
    modo.add_argument(
        "--check",
        action="store_true",
        help="riscarica lo storico salvato e riporta le differenze, senza scrivere",
    )
    parser.add_argument("--start", default=START, help="data di inizio per il download completo")
    parser.add_argument("--source", choices=SORGENTI, default=SORGENTE_DEFAULT, help="fornitore dei dati")
    return parser.parse_args(argv)


def cli(argv: list[str] | None = None) -> None:
    """Punto di ingresso da riga di comando: esce con 1 se un simbolo e' bloccato, in errore o in deriva."""
    from quant.live.alerts import build_alerter

    argomenti = parse_args(sys.argv[1:] if argv is None else argv)
    if argomenti.check:
        from quant.drift import run_check

        controllo = run_check(
            crea_sorgente(argomenti.source), argomenti.symbols or None, alerter=build_alerter()
        )
        raise SystemExit(controllo.exit_code)
    risultato = run_download(
        crea_sorgente(argomenti.source),
        argomenti.symbols or None,
        start=argomenti.start,
        update=argomenti.update,
        calendar_provider=alpaca_calendar(),
        alerter=build_alerter(),
    )
    raise SystemExit(risultato.exit_code)


if __name__ == "__main__":
    cli()
