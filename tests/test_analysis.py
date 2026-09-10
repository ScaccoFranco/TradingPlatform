"""Test delle metriche su equity curve costruite a mano."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
from pytest import approx

from quant.analysis import compare, compute_metrics, max_drawdown, to_series
from quant.events import FillEvent, OrderDirection


def circa(valore: float, tolleranza: float = 1e-9) -> object:
    """Confronto numerico con tolleranza."""
    return approx(valore, rel=tolleranza, abs=tolleranza)


def curva(valori: list[float], inizio: str = "2020-01-01") -> list[tuple[datetime, float]]:
    """Equity curve su giorni lavorativi consecutivi."""
    date = pd.bdate_range(inizio, periods=len(valori))
    return [(d.to_pydatetime(), v) for d, v in zip(date, valori)]


def fill(commissione: float) -> FillEvent:
    """Eseguito fittizio con la commissione indicata."""
    return FillEvent(datetime(2020, 1, 2), "SPY", OrderDirection.BUY, 10, 100.0, commissione)


def test_crescita_costante_da_cagr_atteso_e_vol_nulla() -> None:
    metriche = compute_metrics(curva([100 * 1.001**i for i in range(253)]))
    assert metriche["rendimento_totale"] == circa(0.286434, 1e-5)
    assert metriche["cagr"] == circa(0.298689, 1e-5)
    assert metriche["volatilita"] == circa(0.0, 1e-12)
    assert metriche["sharpe"] == 0.0
    assert metriche["max_drawdown"] == 0.0


def test_sharpe_e_volatilita_su_rendimenti_noti() -> None:
    metriche = compute_metrics(curva([100.0, 101.0, 103.02, 104.0502, 106.131204]))
    assert metriche["sharpe"] == circa(41.243181, 1e-5)
    assert metriche["volatilita"] == circa(0.091652, 1e-5)
    assert metriche["rendimento_totale"] == circa(0.061312, 1e-6)


def test_max_drawdown_su_picco_e_valle() -> None:
    metriche = compute_metrics(curva([100.0, 110.0, 88.0, 99.0]))
    assert metriche["max_drawdown"] == circa(-0.2, 1e-12)
    assert max_drawdown(to_series(curva([100.0, 110.0, 88.0, 99.0]))) == circa(-0.2, 1e-12)


def test_trade_e_costi_dai_fill() -> None:
    metriche = compute_metrics(curva([100.0, 101.0, 102.0]), [fill(1.5), fill(2.5)])
    assert metriche["n_trade"] == 2
    assert metriche["costi_totali"] == circa(4.0, 1e-12)


def test_curva_troppo_corta_non_solleva() -> None:
    assert compute_metrics([])["sharpe"] == 0.0
    assert compute_metrics(curva([100.0]))["cagr"] == 0.0


def test_compare_stampa_le_due_colonne(capsys) -> None:
    tabella = compare(
        compute_metrics(curva([100.0, 105.0, 110.0])),
        compute_metrics(curva([100.0, 99.0, 98.0])),
        labels=("momentum", "SPY"),
    )
    assert "momentum" in tabella and "SPY" in tabella
    assert "Sharpe (rf=0)" in tabella
    assert tabella in capsys.readouterr().out
