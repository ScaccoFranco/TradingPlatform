"""Manifest dei dati: per ogni simbolo sorgente, data di download, intervallo, barre e SHA-256.

Vive in `data/manifest.json`, accanto alla cartella dei Parquet, ed e' versionato in git:
i Parquet no, quindi il manifest e' la sola traccia di quali dati hanno prodotto un
risultato. Lo aggiorna ogni download, mai a mano.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

import pandas as pd

MANIFEST_NAME = "manifest.json"
VERSIONE = 1


class ProvenanceError(RuntimeError):
    """I Parquet su disco non corrispondono a quanto registrato nel manifest."""


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """Voce del manifest per un simbolo."""

    source: str
    downloaded_at: str
    first: str
    last: str
    bars: int
    sha256: str


def manifest_path(data_dir: str | Path) -> Path:
    """Il manifest sta nella cartella che contiene quella dei Parquet: `data/parquet` -> `data/`."""
    return Path(data_dir).parent / MANIFEST_NAME


def sha256_file(path: str | Path) -> str:
    """SHA-256 del contenuto del file, ricalcolato solo se dimensione o data di modifica cambiano."""
    stato = Path(path).stat()
    return _sha256(str(Path(path).resolve()), stato.st_size, stato.st_mtime_ns)


@lru_cache(maxsize=256)
def _sha256(percorso: str, dimensione: int, modificato: int) -> str:
    impronta = hashlib.sha256()
    with open(percorso, "rb") as file:
        for blocco in iter(lambda: file.read(1 << 20), b""):
            impronta.update(blocco)
    return impronta.hexdigest()


def load_manifest(path: str | Path) -> dict[str, ManifestEntry]:
    """Voci del manifest per simbolo; vuoto se il file non esiste ancora."""
    percorso = Path(path)
    if not percorso.exists():
        return {}
    contenuto = json.loads(percorso.read_text(encoding="utf-8"))
    return {simbolo: ManifestEntry(**voce) for simbolo, voce in contenuto.get("symbols", {}).items()}


def save_manifest(entries: dict[str, ManifestEntry], path: str | Path) -> Path:
    """Scrive il manifest in modo atomico, con chiavi ordinate perche' i diff in git restino leggibili."""
    percorso = Path(path)
    percorso.parent.mkdir(parents=True, exist_ok=True)
    contenuto = {"version": VERSIONE, "symbols": {s: asdict(entries[s]) for s in sorted(entries)}}
    temporaneo = percorso.with_name(f".{percorso.name}.tmp")
    temporaneo.write_text(json.dumps(contenuto, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporaneo, percorso)
    return percorso


def manifest_sha256(path: str | Path) -> str | None:
    """Hash del manifest, None se non esiste."""
    percorso = Path(path)
    return sha256_file(percorso) if percorso.exists() else None


def entry_for(parquet: str | Path, source: str, giorno: date) -> ManifestEntry:
    """Voce del manifest per un Parquet appena scritto."""
    indice = pd.read_parquet(parquet, columns=[]).index
    return ManifestEntry(
        source=source,
        downloaded_at=giorno.isoformat(),
        first=pd.Timestamp(indice.min()).date().isoformat(),
        last=pd.Timestamp(indice.max()).date().isoformat(),
        bars=len(indice),
        sha256=sha256_file(parquet),
    )


def verify(symbol: str, parquet: str | Path, entries: dict[str, ManifestEntry], source: str) -> ManifestEntry:
    """Controlla che un Parquet da aggiornare sia quello registrato e venga dalla stessa sorgente.

    Accodare barre di un altro fornitore, o a un file toccato fuori dal download, darebbe
    una serie di cui il manifest non sa piu' rendere conto.
    """
    voce = entries.get(symbol)
    if voce is None:
        raise ProvenanceError(
            f"{symbol}: assente dal manifest, provenienza ignota: serve un download completo"
        )
    if voce.source != source:
        raise ProvenanceError(
            f"{symbol}: scaricato da {voce.source}, aggiornamento da {source}: serve un download completo"
        )
    if voce.sha256 != sha256_file(parquet):
        raise ProvenanceError(
            f"{symbol}: Parquet diverso da quello nel manifest, modificato fuori dal download"
        )
    return voce
