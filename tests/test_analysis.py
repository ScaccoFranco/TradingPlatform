"""Test delle metriche su equity curve costruite a mano."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
from pytest import approx

from quant.analysis import (
    compare,
    compute_metrics,
    drawdown_series,
    format_metric,
    format_table,
    max_drawdown,
    metric_label,
    to_series,
)
from quant.events import FillEvent, OrderDirection


def circa(valore: float, tolleranza: float = 1e-9) -> object:
    """Confronto numerico con tolleranza."""
    return approx(valore, rel=tolleranza, abs=tolleranza)


def curva(valori: list[float], inizio: str = "2020-01-01") -> list[tuple[datetime, float]]:
    """Equity curve su giorni lavorativi consecutivi."""
    date = pd.bdate_range(inizio, periods=len(valori))
    return [(d.to_pydatetime(), v) for d, v in zip(date, valori, strict=False)]


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


def rendimenti_di(curva: list[tuple[datetime, float]]) -> pd.Series:
    """Rendimenti giornalieri di una equity curve, indicizzati per data."""
    serie = to_series(curva)
    return serie.pct_change().dropna()


def test_sharpe_in_eccesso_nullo_se_il_tasso_uguaglia_la_strategia() -> None:
    """Sottraendo alla strategia se stessa non resta nessuna sovraperformance."""
    valori = [100.0, 101.0, 100.5, 103.0, 102.0, 105.0]
    curva = curva_da(valori)
    metriche = compute_metrics(curva, risk_free=rendimenti_di(curva))

    assert metriche["sharpe"] == 0.0
    assert metriche["sortino"] == 0.0
    assert metriche["excess"] == 1.0
    assert metriche["volatilita"] == compute_metrics(curva)["volatilita"]
    assert metriche["cagr"] == compute_metrics(curva)["cagr"]


def test_il_tasso_privo_di_rischio_abbassa_lo_sharpe() -> None:
    curva = curva_da([100.0, 101.0, 100.5, 103.0, 102.0, 105.0])
    lordo = compute_metrics(curva)
    netto = compute_metrics(curva, risk_free=pd.Series(0.0005, index=to_series(curva).index))

    assert netto["sharpe"] < lordo["sharpe"]
    assert netto["max_drawdown"] == lordo["max_drawdown"]
    assert netto["calmar"] == lordo["calmar"]


def test_sortino_ignora_la_volatilita_al_rialzo() -> None:
    """Due curve con la stessa media ma perdite diverse hanno Sortino diverso."""
    regolare = compute_metrics(curva_da([100.0, 101.0, 102.0, 103.0, 104.0, 103.0]))
    scossa = compute_metrics(curva_da([100.0, 96.0, 104.0, 99.0, 108.0, 103.0]))

    assert regolare["sortino"] > scossa["sortino"]
    assert regolare["sortino"] > regolare["sharpe"]


def test_calmar_e_cagr_su_drawdown() -> None:
    metriche = compute_metrics(curva_da([100.0, 110.0, 88.0, 99.0]))
    assert metriche["calmar"] == circa(metriche["cagr"] / abs(metriche["max_drawdown"]))


def test_etichetta_della_tabella_cambia_con_il_tasso() -> None:
    curva = curva_da([100.0, 101.0, 102.0])
    assert "Sharpe (rf=0)" in format_table({"a": compute_metrics(curva)})
    tasso = pd.Series(0.0001, index=to_series(curva).index)
    assert "Sharpe (excess)" in format_table({"a": compute_metrics(curva, risk_free=tasso)})


def test_drawdown_giorno_per_giorno_e_suo_minimo() -> None:
    equity = to_series(curva([100.0, 110.0, 99.0, 121.0]))
    assert list(drawdown_series(equity)) == approx([0.0, 0.0, -0.1, 0.0])
    assert max_drawdown(equity) == circa(-0.1)
    assert drawdown_series(pd.Series(dtype="float64")).empty


def test_etichette_e_formati_delle_metriche() -> None:
    assert metric_label("sharpe") == ("Sharpe (rf=0)", "num")
    assert metric_label("sharpe", in_eccesso=True) == ("Sharpe (excess)", "num")
    assert metric_label("max_drawdown") == ("Max drawdown", "pct")
    assert format_metric(-0.02, "pct") == "-2.00%"
    assert format_metric(1234.4, "cur") == "1,234"


def curva_da(valori: list[float]) -> list[tuple[datetime, float]]:
    """Equity curve su giorni lavorativi consecutivi."""
    return curva(valori)
