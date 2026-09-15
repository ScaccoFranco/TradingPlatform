"""Grafici della dashboard: SVG in memoria, nessun file, nessuna figura lasciata aperta."""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import pytest

from quant.analysis import drawdown_series
from quant.webui.charts import grafico_drawdown, grafico_equity, grafico_esposizione


def serie(valori: list[float]) -> pd.Series:
    """Serie su giorni lavorativi consecutivi."""
    return pd.Series(valori, index=pd.bdate_range("2020-01-01", periods=len(valori)), dtype="float64")


def test_grafici_sono_svg_in_memoria(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    live = serie([100.0, 101.0, 99.0, 102.0, 103.0])
    curve = {
        "live": live,
        "shadow": serie([100.0, 100.5, 99.5, 101.5, 102.0]),
        "SPY": serie([4.0, 4.1, 4.0, 4.2, 4.3]),
    }

    equity = grafico_equity(curve)
    drawdown = grafico_drawdown({nome: drawdown_series(valori) for nome, valori in curve.items()})
    esposizione = grafico_esposizione(serie([0.95, 0.97, 0.96, 0.98, 0.99]))

    for svg in (equity, drawdown, esposizione):
        assert svg is not None
        assert svg.startswith("<svg ") and svg.rstrip().endswith("</svg>")
        assert 'role="img"' in svg and "aria-label=" in svg
        assert "<script" not in svg and "<?xml" not in svg
    assert equity is not None and esposizione is not None
    assert all(f"var(--serie-{slot})" in equity for slot in (1, 2, 3))
    assert "#2a78d6" not in equity, "i colori devono seguire il tema della pagina"
    assert "var(--serie-2)" not in esposizione

    assert list(tmp_path.iterdir()) == [], "nessun file scritto su disco"
    assert plt.get_fignums() == [], "nessuna figura registrata in pyplot"


def test_serie_troppo_corte_non_danno_grafico() -> None:
    assert grafico_equity({"live": serie([100.0])}) is None
    assert grafico_drawdown({}) is None
    assert grafico_esposizione(pd.Series(dtype="float64")) is None
