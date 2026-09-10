"""Impianto condiviso dagli script di analisi: universo, costi e confronto a tre."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from quant.analysis import format_table, to_series
from quant.strategies.fixed_weights import FixedWeightsStrategy
from quant.strategies.momentum import CrossSectionalMomentum
from quant.validation import run_backtest

UNIVERSO = ["SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "TLT", "IEF", "LQD", "HYG", "GLD", "DBC"]
CASH = "SHY"
SIMBOLI = [*UNIVERSO, CASH]
DATI = Path("data/parquet")
REPORT = Path("reports")

CAPITALE = 100_000.0
COSTI = {
    "commission_per_trade": 1.0,
    "slippage_bps": 5.0,
    "initial_cash": CAPITALE,
    "cash_buffer": 0.01,
    "credit_dividends": True,
    "max_weight_per_symbol": 1.0,  # non deve vincolare: un tetto piu' basso azzoppa i benchmark
    "path": DATI,
}
PESI_60_40 = {"SPY": 0.6, "IEF": 0.4}


def momentum_factory(params: dict | None = None):
    """Strategy factory del momentum con i parametri di default piu' eventuali override."""
    parametri = dict(params or {})
    return lambda: CrossSectionalMomentum(None, UNIVERSO, cash_symbol=CASH, **parametri)


def confronto(
    start: str | datetime,
    end: str | datetime,
    warmup_start: str | datetime | None = None,
) -> dict[str, dict]:
    """Momentum, SPY e 60/40 nello stesso motore, con gli stessi costi e la stessa cadenza.

    Anche il SPY viene ritargettato al 100% ogni mese: senza ribilanciamento le cedole
    resterebbero ferme in cassa per anni, mentre le altre due serie le reinvestono, e il
    confronto premierebbe la strategia attiva per un motivo che non c'entra col segnale.
    """
    strategie = {
        "momentum": momentum_factory(),
        "SPY mensile": lambda: FixedWeightsStrategy(None, {"SPY": 1.0}),
        "60/40 mensile": lambda: FixedWeightsStrategy(None, PESI_60_40),
    }
    return {
        nome: run_backtest(factory, SIMBOLI, start, end, warmup_start=warmup_start, **COSTI)
        for nome, factory in strategie.items()
    }


def tabella(risultati: dict[str, dict]) -> str:
    """Tabella di confronto delle metriche."""
    return format_table({nome: r["metrics"] for nome, r in risultati.items()})


def salva_grafico(risultati: dict[str, dict], destinazione: Path, titolo: str) -> Path:
    """Equity curve in scala logaritmica, una linea per serie."""
    destinazione.parent.mkdir(parents=True, exist_ok=True)
    figura, asse = plt.subplots(figsize=(11, 6))
    for nome, risultato in risultati.items():
        serie = to_series(risultato["equity_curve"])
        asse.plot(serie.index, serie.values, label=nome, linewidth=1.2)
    asse.set_yscale("log")
    asse.set_title(titolo)
    asse.set_ylabel("equity (scala log)")
    asse.grid(True, which="both", alpha=0.25)
    asse.legend()
    figura.tight_layout()
    figura.savefig(destinazione, dpi=130)
    plt.close(figura)
    return destinazione


def riepilogo_costi(risultati: dict[str, dict]) -> str:
    """Riga sintetica su trade e costi sostenuti da ogni serie."""
    parti: Sequence[str] = [
        f"{nome}: {int(r['metrics']['n_trade'])} trade, costi {r['metrics']['costi_totali']:,.0f}"
        for nome, r in risultati.items()
    ]
    return " | ".join(parti)
