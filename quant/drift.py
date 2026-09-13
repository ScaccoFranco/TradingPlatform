"""Deriva dei dati: riscarica l'intervallo gia' salvato e lo confronta barra per barra, senza scrivere.

Le barre gia' nei Parquet sono quelle su cui girano i backtest, e la loro impronta finisce
nella provenienza. Se il fornitore le cambia a posteriori, il download incrementale non se
ne accorge: questo controllo si'. Una differenza non si corregge in silenzio: si riporta in
`reports/data_drift_YYYY-MM-DD.csv`, si esce con codice diverso da zero e parte l'alert. Il
CSV porta lo SHA-256 del Parquet confrontato, lo stesso della provenienza dei backtest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quant.download import NEW_YORK, OPTIONAL, OUTPUT_DIR, REPORT_DIR, SYMBOLS, scarica
from quant.manifest import load_manifest, manifest_path, sha256_file
from quant.sources import DataSource, DataSourceError, SchemaError, valida_simbolo

COLONNE_GREZZE = ("open", "high", "low", "close", "volume", "dividends", "split_factor")
CSV_COLUMNS = [
    "symbol",
    "date",
    "kind",
    "column",
    "stored",
    "fetched",
    "relative_change",
    "stored_source",
    "check_source",
    "parquet_sha256",
]
TOLLERANZA_RELATIVA = 1e-6
TOLLERANZA_ASSOLUTA = 1e-9


def compare(
    stored: pd.DataFrame,
    fetched: pd.DataFrame,
    symbol: str,
    rtol: float = TOLLERANZA_RELATIVA,
    atol: float = TOLLERANZA_ASSOLUTA,
) -> pd.DataFrame:
    """Differenze fra barre salvate e riscaricate sull'intervallo del Parquet.

    `stored` e' nello schema dei Parquet, `fetched` nel contratto delle sorgenti. Si
    confrontano solo i valori grezzi: `adj_close` e' derivato, e cambia per costruzione a
    ogni nuova cedola. Tre tipi: `changed` per un valore diverso, `removed` per una barra
    che la sorgente non ha piu', `added` per una barra nel passato che prima non c'era.
    """
    riscaricate = fetched.rename(columns={"dividend": "dividends", "split": "split_factor"})
    if not stored.empty:
        dentro = (riscaricate.index >= stored.index.min()) & (riscaricate.index <= stored.index.max())
        riscaricate = riscaricate[dentro]
    righe: list[dict[str, Any]] = []
    for giorno in stored.index.difference(riscaricate.index):
        righe.append({"symbol": symbol, "date": giorno, "kind": "removed"})
    for giorno in riscaricate.index.difference(stored.index):
        righe.append({"symbol": symbol, "date": giorno, "kind": "added"})

    comuni = stored.index.intersection(riscaricate.index)
    for colonna in COLONNE_GREZZE:
        salvati = stored.loc[comuni, colonna].astype("float64")
        nuovi = riscaricate.loc[comuni, colonna].astype("float64")
        vicini = (salvati - nuovi).abs() <= atol + rtol * nuovi.abs()
        diversi = ~(vicini | (salvati.isna() & nuovi.isna()))
        for giorno, prima, dopo in zip(comuni[diversi], salvati[diversi], nuovi[diversi], strict=True):
            relativa = dopo / prima - 1.0 if prima else float("nan")
            righe.append(
                {
                    "symbol": symbol,
                    "date": giorno,
                    "kind": "changed",
                    "column": colonna,
                    "stored": prima,
                    "fetched": dopo,
                    "relative_change": relativa,
                }
            )
    differenze = pd.DataFrame(righe, columns=CSV_COLUMNS)
    return differenze.sort_values(["date", "kind", "column"], na_position="first", ignore_index=True)


@dataclass(slots=True)
class DriftResult:
    """Esito di `--check`: differenze trovate, errori per simbolo, CSV scritto."""

    differences: pd.DataFrame
    csv_path: Path
    errors: dict[str, str] = field(default_factory=dict)
    checked: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        """True con una differenza retroattiva o un simbolo obbligatorio non verificabile."""
        obbligatori = [s for s in self.errors if s not in OPTIONAL]
        return not self.differences.empty or bool(obbligatori)

    @property
    def exit_code(self) -> int:
        """Codice di uscita del processo."""
        return 1 if self.failed else 0


def run_check(
    source: DataSource,
    symbols: list[str] | None = None,
    *,
    output_dir: Path = OUTPUT_DIR,
    report_dir: Path = REPORT_DIR,
    oggi: date | None = None,
    alerter: Any = None,
) -> DriftResult:
    """Riscarica ogni Parquet presente sul suo intervallo e riporta le differenze, senza scrivere dati.

    Se la sorgente del controllo non e' quella del manifest il confronto e' fra fornitori:
    le differenze dicono quanto divergono, non che uno dei due ha cambiato il passato.
    """
    giorno = oggi or datetime.now(NEW_YORK).date()
    registro = load_manifest(manifest_path(output_dir))
    parti: list[pd.DataFrame] = []
    risultato = DriftResult(pd.DataFrame(columns=CSV_COLUMNS), report_dir / f"data_drift_{giorno}.csv")
    for symbol in [valida_simbolo(s) for s in symbols or SYMBOLS]:
        parquet = output_dir / f"{symbol}.parquet"
        if not parquet.exists():
            print(f"{symbol}: nessun Parquet da verificare")
            continue
        salvate = pd.read_parquet(parquet)
        origine = registro[symbol].source if symbol in registro else "sconosciuta"
        try:
            inizio, fine = pd.Timestamp(salvate.index.min()).date(), pd.Timestamp(salvate.index.max()).date()
            riscaricate = scarica(source, symbol, inizio, fine, oggi=giorno)
        except (DataSourceError, SchemaError) as errore:
            risultato.errors[symbol] = str(errore)
            print(f"{symbol}: non verificabile ({errore})")
            continue
        differenze = compare(salvate, riscaricate, symbol)
        differenze["stored_source"] = origine
        differenze["check_source"] = source.name
        differenze["parquet_sha256"] = sha256_file(parquet)
        risultato.checked.append(symbol)
        parti.append(differenze)
        print(f"{symbol}: {_conteggio(differenze)} su {len(salvate)} barre ({origine} contro {source.name})")

    if parti:
        risultato.differences = pd.concat(parti, ignore_index=True)[CSV_COLUMNS]
    report_dir.mkdir(parents=True, exist_ok=True)
    uscita = risultato.differences.copy()
    uscita["date"] = pd.to_datetime(uscita["date"]).dt.date.astype("string")
    uscita.to_csv(risultato.csv_path, index=False)
    print(f"deriva: {risultato.csv_path}")
    if risultato.failed and alerter is not None:
        alerter.send("error", _testo_alert(risultato, giorno))
    return risultato


def _conteggio(differenze: pd.DataFrame) -> str:
    if differenze.empty:
        return "nessuna differenza"
    conteggi = differenze["kind"].value_counts()
    return ", ".join(
        f"{int(conteggi.get(k, 0))} {k}" for k in ("changed", "removed", "added") if k in conteggi
    )


def _testo_alert(risultato: DriftResult, giorno: date) -> str:
    parti = []
    if not risultato.differences.empty:
        simboli = sorted(risultato.differences["symbol"].unique())
        parti.append(f"differenze retroattive su {', '.join(simboli)}")
    obbligatori = sorted(s for s in risultato.errors if s not in OPTIONAL)
    if obbligatori:
        parti.append(f"non verificabili {', '.join(obbligatori)}")
    return f"controllo deriva dati {giorno}: {'; '.join(parti)}. Dettaglio {risultato.csv_path}"
