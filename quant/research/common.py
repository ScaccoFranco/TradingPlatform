"""Impianto condiviso degli script di ricerca: universo, costi e confronto a tre."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import logging

import matplotlib.pyplot as plt

from quant.analysis import format_table, to_series
from quant.config import PERCORSO_DATI, BacktestConfig
from quant.logging import configure
from quant.provenance import provenance_markdown
from quant.strategies.fixed_weights import FixedWeightsStrategy
from quant.strategies.momentum import CrossSectionalMomentum
from quant.strategy import Strategy
from quant.validation import BacktestResult, run_backtest

UNIVERSO = ["SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "TLT", "IEF", "LQD", "HYG", "GLD", "DBC"]
CASH = "SHY"
SIMBOLI = [*UNIVERSO, CASH]
REPORT = Path("reports")

CONFIG = BacktestConfig(
    initial_cash=100_000.0,
    commission_per_trade=1.0,
    slippage_bps=5.0,
    cash_buffer=0.01,
    dividends_as_cash=True,
    max_weight_per_symbol=1.0,  # non deve vincolare: un tetto piu' basso azzoppa i benchmark
    risk_free_symbol=CASH,
    path=PERCORSO_DATI,
)
PESI_60_40 = {"SPY": 0.6, "IEF": 0.4}


class Resoconto:
    """Testo di uno script di ricerca: stampato a video e salvato in `reports/` con la provenienza in coda."""

    def __init__(self, nome: str) -> None:
        self.nome = nome
        self.righe: list[str] = []
        self.backtest: dict[str, BacktestResult] = {}

    def scrivi(self, testo: object = "") -> None:
        """Stampa e conserva una riga del resoconto."""
        print(testo)
        self.righe.append(str(testo))

    def tabella(self, testo: str) -> None:
        """Una tabella a larghezza fissa, dentro un blocco di codice nel markdown."""
        print(testo)
        self.righe.extend(["```", testo, "```"])

    def registra(self, risultati: dict[str, BacktestResult], prefisso: str = "") -> None:
        """Backtest di cui riportare la provenienza in coda."""
        self.backtest.update({f"{prefisso}{nome}": r for nome, r in risultati.items()})

    def salva(self, cartella: Path = REPORT) -> Path:
        """Scrive `reports/<nome>.md`: testo e, in coda, la provenienza di ogni backtest."""
        cartella.mkdir(parents=True, exist_ok=True)
        destinazione = cartella / f"{self.nome}.md"
        corpo = "\n".join(self.righe)
        destinazione.write_text(f"{corpo}\n\n{provenance_markdown(self.backtest)}", encoding="utf-8")
        print(f"resoconto salvato in {destinazione}")
        return destinazione


def quiet_logging() -> None:
    """Negli script di ricerca bastano gli avvisi: un fill per riga coprirebbe le tabelle."""
    configure(level=logging.WARNING, force=True)


def momentum_factory(params: dict[str, Any] | None = None) -> Callable[[], Strategy]:
    """Strategy factory del momentum con i parametri di default piu' eventuali override."""
    parametri = dict(params or {})
    return lambda: CrossSectionalMomentum(None, UNIVERSO, cash_symbol=CASH, **parametri)


def confronto(
    start: str | datetime,
    end: str | datetime,
    warmup_start: str | datetime | None = None,
    config: BacktestConfig = CONFIG,
) -> dict[str, BacktestResult]:
    """Momentum, SPY e 60/40 nello stesso motore, con gli stessi costi e la stessa cadenza.

    Anche il SPY viene ritargettato al 100% ogni mese: senza ribilanciamento le cedole
    resterebbero ferme in cassa per anni, mentre le altre due serie le reinvestono, e il
    confronto premierebbe la strategia attiva per un motivo che non c'entra col segnale.
    """
    strategie: dict[str, Callable[[], Strategy]] = {
        "momentum": momentum_factory(),
        "SPY mensile": lambda: FixedWeightsStrategy(None, {"SPY": 1.0}),
        "60/40 mensile": lambda: FixedWeightsStrategy(None, PESI_60_40),
    }
    return {
        nome: run_backtest(factory, SIMBOLI, start, end, config=config, warmup_start=warmup_start)
        for nome, factory in strategie.items()
    }


def tabella(risultati: dict[str, BacktestResult]) -> str:
    """Tabella di confronto delle metriche."""
    return format_table({nome: r["metrics"] for nome, r in risultati.items()})


def salva_grafico(risultati: dict[str, BacktestResult], destinazione: Path, titolo: str) -> Path:
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


def riepilogo_costi(risultati: dict[str, BacktestResult]) -> str:
    """Riga sintetica su trade e costi sostenuti da ogni serie."""
    parti: Sequence[str] = [
        f"{nome}: {int(r['metrics']['n_trade'])} trade, costi {r['metrics']['costi_totali']:,.0f}"
        for nome, r in risultati.items()
    ]
    return " | ".join(parti)
