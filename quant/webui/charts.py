"""Grafici della dashboard: figure matplotlib rese come SVG dentro una stringa.

Le figure si costruiscono con `matplotlib.figure.Figure`, senza pyplot: nessun registro
globale di figure aperte, niente che passi da una chiamata all'altra, nessun file su
disco. I colori escono come variabili CSS definite in `static/stile.css`, cosi' il
grafico segue il tema chiaro o scuro della pagina. Ogni valore disegnato sta anche
nella tabella della pagina: il colore non e' mai l'unico modo di leggerlo.
"""

from __future__ import annotations

import html
import io
import threading
from collections.abc import Mapping
from typing import Any

import matplotlib

matplotlib.use("Agg")
import pandas as pd
from matplotlib.dates import AutoDateLocator, ConciseDateFormatter
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter, MaxNLocator, NullLocator, PercentFormatter

from quant.weekly import BENCHMARK, rebase

# Colori della palette chiara usati come segnaposto: nel SVG finale diventano variabili CSS.
SERIE = {"live": "#2a78d6", "shadow": "#eb6834", BENCHMARK: "#1baf7a"}
TESTO = "#52514e"
TENUE = "#898781"
GRIGLIA = "#e1e0d9"
ASSE = "#c3c2b7"
VARIABILI = {
    SERIE["live"]: "serie-1",
    SERIE["shadow"]: "serie-2",
    SERIE[BENCHMARK]: "serie-3",
    TESTO: "grafico-testo",
    TENUE: "grafico-tenue",
    GRIGLIA: "grafico-griglia",
    ASSE: "grafico-asse",
}
SPESSORE = 1.6
_BLOCCO = threading.Lock()  # matplotlib non garantisce disegni concorrenti; FastAPI usa un pool di thread


def grafico_equity(serie: Mapping[str, pd.Series]) -> str | None:
    """Curve a base 100 in scala logaritmica: conta la forma, non il livello."""
    curve = {nome: rebase(valori) for nome, valori in serie.items()}
    curve = {nome: valori for nome, valori in curve.items() if len(valori) > 1}
    if not curve:
        return None
    with _BLOCCO:
        figura, asse = _figura()
        for nome, valori in curve.items():
            _linea(asse, valori, nome)
        asse.set_yscale("log")
        asse.yaxis.set_major_locator(MaxNLocator(nbins=5))
        asse.yaxis.set_minor_locator(NullLocator())
        asse.yaxis.set_major_formatter(FuncFormatter(_base_cento))
        _legenda(asse)
        return _svg(figura, f"Equity di {', '.join(curve)} a base 100, scala logaritmica")


def grafico_drawdown(serie: Mapping[str, pd.Series]) -> str | None:
    """Distanza dal picco precedente, una linea per serie sulla stessa scala."""
    curve = {nome: valori for nome, valori in serie.items() if len(valori) > 1}
    if not curve:
        return None
    with _BLOCCO:
        figura, asse = _figura()
        asse.axhline(0.0, color=ASSE, linewidth=0.8)
        for nome, valori in curve.items():
            _linea(asse, valori, nome)
        asse.yaxis.set_major_formatter(PercentFormatter(xmax=1.0, decimals=1))
        _legenda(asse)
        return _svg(figura, f"Drawdown di {', '.join(curve)}")


def grafico_esposizione(esposizione: pd.Series) -> str | None:
    """Esposizione del live in frazione dell'equity: una serie sola, il titolo la nomina."""
    if len(esposizione) < 2:
        return None
    with _BLOCCO:
        figura, asse = _figura()
        asse.fill_between(
            esposizione.index, esposizione.to_numpy(), 0.0, color=SERIE["live"], alpha=0.1, linewidth=0
        )
        _linea(asse, esposizione, "live")
        asse.set_ylim(bottom=0.0)
        asse.yaxis.set_major_formatter(PercentFormatter(xmax=1.0, decimals=0))
        return _svg(figura, "Esposizione del live in percentuale dell'equity")


def _figura() -> tuple[Figure, Any]:
    """Figura con assi e griglia recessivi e date concise sull'asse orizzontale."""
    figura = Figure(figsize=(9.0, 3.0), layout="constrained")
    asse = figura.add_subplot()
    for lato in ("top", "right", "left"):
        asse.spines[lato].set_visible(False)
    asse.spines["bottom"].set_color(ASSE)
    asse.grid(True, axis="y", color=GRIGLIA, linewidth=0.8)
    asse.set_axisbelow(True)
    asse.tick_params(colors=TENUE, labelsize=9, length=0, pad=6)
    date = AutoDateLocator(minticks=3, maxticks=8)  # type: ignore[no-untyped-call]
    asse.xaxis.set_major_locator(date)
    asse.xaxis.set_major_formatter(ConciseDateFormatter(date))  # type: ignore[no-untyped-call]
    asse.xaxis.get_offset_text().set_color(TENUE)
    return figura, asse


def _linea(asse: Any, valori: pd.Series, nome: str) -> None:
    """Una serie come linea sottile nel colore della sua entita', lo stesso in ogni grafico."""
    asse.plot(
        valori.index,
        valori.to_numpy(),
        color=SERIE[nome],
        linewidth=SPESSORE,
        label=nome,
        solid_joinstyle="round",
        solid_capstyle="round",
    )


def _legenda(asse: Any) -> None:
    """Legenda senza cornice sopra il grafico, presente solo con due serie o piu'."""
    _, etichette = asse.get_legend_handles_labels()
    if len(etichette) < 2:
        return
    asse.legend(
        loc="lower left",
        bbox_to_anchor=(0.0, 1.0),
        ncols=len(etichette),
        frameon=False,
        fontsize=9,
        labelcolor=TESTO,
        handlelength=1.4,
        borderaxespad=0.2,
    )


def _base_cento(valore: float, _posizione: Any) -> str:
    """Etichetta di un livello a base 100."""
    return f"{valore:,.1f}"


def _svg(figura: Figure, descrizione: str) -> str:
    """SVG da mettere inline: niente intestazione XML, colori come variabili CSS, etichetta accessibile."""
    buffer = io.StringIO()
    figura.savefig(buffer, format="svg", transparent=True, metadata={"Date": None, "Creator": None})
    testo = buffer.getvalue()
    testo = testo[testo.index("<svg") :]
    for colore, variabile in VARIABILI.items():
        testo = testo.replace(colore, f"var(--{variabile})")
    etichetta = html.escape(descrizione, quote=True)
    return testo.replace("<svg ", f'<svg role="img" aria-label="{etichetta}" ', 1)
