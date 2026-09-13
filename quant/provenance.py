"""Provenienza di un backtest: quali dati, quale codice e quali parametri l'hanno prodotto."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from quant.config import BacktestConfig
from quant.manifest import load_manifest, manifest_path, manifest_sha256, sha256_file

RADICE = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class Provenance:
    """Blocco `provenance` dei risultati di `run_backtest`.

    `hash` e' l'impronta del backtest: dati usati, commit, stato del working tree,
    configurazione, finestra e strategia. Due backtest con la stessa impronta devono
    dare lo stesso risultato. L'hash del manifest si riporta ma resta fuori dall'impronta,
    perche' cambia anche quando si aggiorna un simbolo che il backtest non usa: contano gli
    hash dei Parquet letti davvero, in `data_sha256`.
    """

    hash: str
    manifest_sha256: str | None
    data_sha256: dict[str, str | None]
    manifest_mismatch: tuple[str, ...]
    git_commit: str | None
    git_dirty: bool | None
    config: dict[str, Any]
    run: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Versione serializzabile in JSON."""
        return asdict(self)

    def to_markdown(self, title: str = "## Provenienza") -> str:
        """Sezione da mettere in coda a un report."""
        commit = self.git_commit or "non disponibile"
        if self.git_dirty:
            commit += ", con modifiche non committate"
        dati = ", ".join(f"{s} `{(h or 'assente')[:12]}`" for s, h in sorted(self.data_sha256.items()))
        parametri = ", ".join(f"{k}={v}" for k, v in sorted(self.config.items()))
        strategia = self.run.get("strategy", {})
        righe = [
            title,
            "",
            f"- Impronta: `{self.hash}`",
            f"- Commit: `{commit}`",
            f"- Manifest: `{self.manifest_sha256 or 'assente'}`",
            f"- Dati: {dati}",
            f"- Finestra: {self.run.get('start')} -> {self.run.get('end')}, "
            f"riscaldamento da {self.run.get('warmup_start')}",
            f"- Strategia: {strategia.get('class')} {strategia.get('params', {})}",
            f"- Config: {parametri}",
        ]
        if self.manifest_mismatch:
            righe.append(f"- **Dati fuori dal manifest**: {', '.join(self.manifest_mismatch)}")
        return "\n".join(righe) + "\n"


def git_state(cartella: Path = RADICE) -> tuple[str | None, bool | None]:
    """Commit corrente e stato del working tree; None se git non c'e' o non e' un repository."""
    try:
        commit = _git(cartella, "rev-parse", "HEAD").strip()
        modifiche = _git(cartella, "status", "--porcelain")
    except (OSError, subprocess.SubprocessError):
        return None, None
    return commit, bool(modifiche.strip())


def _git(cartella: Path, *argomenti: str) -> str:
    esito = subprocess.run(
        ["git", *argomenti], cwd=cartella, capture_output=True, text=True, check=True, timeout=10
    )
    return esito.stdout


def describe_strategy(strategy: object) -> dict[str, Any]:
    """Classe e parametri pubblici della strategia, letti prima che il backtest la faccia girare."""
    parametri: dict[str, Any] = {}
    for nome, valore in sorted(getattr(strategy, "__dict__", {}).items()):
        if nome.startswith("_") or nome == "data_handler":
            continue
        try:
            parametri[nome] = json.loads(json.dumps(valore))
        except (TypeError, ValueError):
            continue
    classe = type(strategy)
    return {"class": f"{classe.__module__}.{classe.__qualname__}", "params": parametri}


def _testo(valore: str | datetime | None) -> str | None:
    return None if valore is None else str(valore)


def build_provenance(
    config: BacktestConfig,
    symbols: Sequence[str],
    start: str | datetime | None,
    end: str | datetime | None,
    warmup_start: str | datetime | None,
    strategy: object,
) -> Provenance:
    """Raccoglie la provenienza di un backtest; l'impronta e' lo SHA-256 di tutto il resto."""
    cartella = Path(config.path)
    registro = load_manifest(manifest_path(cartella))
    dati: dict[str, str | None] = {}
    for symbol in symbols:
        parquet = cartella / f"{symbol}.parquet"
        dati[symbol] = sha256_file(parquet) if parquet.exists() else None
    fuori = tuple(s for s, h in dati.items() if registro and (s not in registro or registro[s].sha256 != h))
    commit, sporco = git_state()
    impostazioni = json.loads(json.dumps(asdict(config), default=str))
    esecuzione = {
        "symbols": list(symbols),
        "start": _testo(start),
        "end": _testo(end),
        "warmup_start": _testo(warmup_start),
        "strategy": describe_strategy(strategy),
    }
    impronta = {
        "data_sha256": dati,
        "git_commit": commit,
        "git_dirty": sporco,
        "config": impostazioni,
        "run": esecuzione,
    }
    testo = json.dumps(impronta, sort_keys=True, default=str)
    return Provenance(
        hash=hashlib.sha256(testo.encode("utf-8")).hexdigest(),
        manifest_sha256=manifest_sha256(manifest_path(cartella)),
        data_sha256=dati,
        manifest_mismatch=fuori,
        git_commit=commit,
        git_dirty=sporco,
        config=impostazioni,
        run=esecuzione,
    )


def provenance_markdown(risultati: dict[str, Any]) -> str:
    """Sezione di provenienza per un report con piu' backtest: una sezione per ognuno."""
    parti = [
        risultato["provenance"].to_markdown(title=f"### Provenienza: {nome}")
        for nome, risultato in risultati.items()
    ]
    return "## Provenienza\n\n" + "\n".join(parti)
