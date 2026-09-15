"""Metriche sull'equity curve e confronto fra strategie."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import pandas as pd

from quant.events import FillEvent

TRADING_DAYS = 252
GIORNI_ANNO = 365.25
EPS_VOL = 1e-12  # sotto questa deviazione i rendimenti sono costanti: Sharpe indefinito, si usa 0


def to_series(equity_curve: Sequence[tuple[datetime, float]]) -> pd.Series:
    """Equity curve come Series indicizzata per data."""
    if not equity_curve:
        return pd.Series(dtype="float64")
    timestamps = pd.DatetimeIndex([t for t, _ in equity_curve])
    return pd.Series([v for _, v in equity_curve], index=timestamps, dtype="float64")


def drawdown_series(equity: pd.Series) -> pd.Series:
    """Distanza dal picco precedente giorno per giorno, in frazione: zero sui massimi."""
    if equity.empty:
        return pd.Series(dtype="float64")
    return equity / equity.cummax() - 1.0


def max_drawdown(equity: pd.Series) -> float:
    """Massima perdita dal picco precedente, in frazione."""
    if equity.empty:
        return 0.0
    return float(drawdown_series(equity).min())


def excess_returns(rendimenti: pd.Series, risk_free: pd.Series | None) -> pd.Series:
    """Rendimenti al netto del tasso privo di rischio, allineati per data.

    Le date senza tasso valgono zero: meglio un giorno contato lordo che un buco
    che accorcerebbe silenziosamente la serie.
    """
    if risk_free is None or risk_free.empty:
        return rendimenti
    allineato = risk_free.reindex(rendimenti.index).fillna(0.0)
    return rendimenti - allineato


def sortino_ratio(eccesso: pd.Series, periods_per_year: int = TRADING_DAYS) -> float:
    """Sortino: come lo Sharpe ma la volatilita' conta solo i rendimenti negativi."""
    if eccesso.empty:
        return 0.0
    negativi = eccesso.clip(upper=0.0)
    downside = float((negativi**2).mean() ** 0.5)
    if downside <= EPS_VOL:
        return 0.0
    return float(eccesso.mean() / downside * (periods_per_year**0.5))


def compute_metrics(
    equity_curve: Sequence[tuple[datetime, float]],
    fills: Sequence[FillEvent] = (),
    periods_per_year: int = TRADING_DAYS,
    risk_free: pd.Series | None = None,
) -> dict[str, float]:
    """CAGR, volatilita', Sharpe, Sortino, Calmar, max drawdown, trade e costi.

    Con `risk_free` Sharpe e Sortino usano i rendimenti in eccesso allineati per data,
    mentre CAGR, volatilita' e drawdown restano lordi: sono grandezze del portafoglio,
    non misure di sovraperformance. Con volatilita' nulla i rapporti valgono 0.
    """
    equity = to_series(equity_curve)
    if len(equity) < 2:
        return _metriche_vuote(fills)

    rendimenti = equity.pct_change().dropna()
    eccesso = excess_returns(rendimenti, risk_free)
    anni = (equity.index[-1] - equity.index[0]).days / GIORNI_ANNO
    rendimento_totale = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1.0 / anni) - 1.0) if anni > 0 else 0.0
    deviazione = float(rendimenti.std(ddof=1)) if len(rendimenti) > 1 else 0.0
    deviazione_eccesso = float(eccesso.std(ddof=1)) if len(eccesso) > 1 else 0.0
    volatilita = 0.0 if deviazione <= EPS_VOL else deviazione * (periods_per_year**0.5)
    sharpe = (
        0.0
        if deviazione_eccesso <= EPS_VOL
        else float(eccesso.mean() / deviazione_eccesso * (periods_per_year**0.5))
    )
    drawdown = max_drawdown(equity)
    calmar = float(cagr / abs(drawdown)) if abs(drawdown) > EPS_VOL else 0.0

    return {
        "sortino": sortino_ratio(eccesso, periods_per_year),
        "calmar": calmar,
        "excess": 1.0 if risk_free is not None and not risk_free.empty else 0.0,
        "rendimento_totale": rendimento_totale,
        "cagr": cagr,
        "volatilita": volatilita,
        "sharpe": sharpe,
        "max_drawdown": drawdown,
        "n_trade": float(len(fills)),
        "commissioni": float(sum(f.commission for f in fills)),
        "slippage": float(sum(f.slippage_cost for f in fills)),
        "costi_totali": float(sum(f.commission + f.slippage_cost for f in fills)),
        "equity_finale": float(equity.iloc[-1]),
        "anni": float(anni),
        "skew": float(rendimenti.skew()) if len(rendimenti) > 2 else 0.0,
        "kurtosis": float(rendimenti.kurtosis() + 3.0) if len(rendimenti) > 3 else 3.0,
        "n_osservazioni": float(len(rendimenti)),
    }


def _metriche_vuote(fills: Sequence[FillEvent]) -> dict[str, float]:
    """Metriche neutre quando l'equity curve e' troppo corta."""
    return {
        "rendimento_totale": 0.0,
        "cagr": 0.0,
        "volatilita": 0.0,
        "sharpe": 0.0,
        "max_drawdown": 0.0,
        "sortino": 0.0,
        "calmar": 0.0,
        "excess": 0.0,
        "n_trade": float(len(fills)),
        "commissioni": float(sum(f.commission for f in fills)),
        "slippage": float(sum(f.slippage_cost for f in fills)),
        "costi_totali": float(sum(f.commission + f.slippage_cost for f in fills)),
        "equity_finale": 0.0,
        "anni": 0.0,
        "skew": 0.0,
        "kurtosis": 3.0,
        "n_osservazioni": 0.0,
    }


ETICHETTE = {
    "rendimento_totale": ("Rendimento totale", "pct"),
    "cagr": ("CAGR", "pct"),
    "volatilita": ("Volatilita' annua", "pct"),
    "sharpe": ("Sharpe (rf=0)", "num"),
    "sortino": ("Sortino", "num"),
    "calmar": ("Calmar", "num"),
    "max_drawdown": ("Max drawdown", "pct"),
    "n_trade": ("Numero di trade", "int"),
    "commissioni": ("Commissioni", "cur"),
    "slippage": ("Slippage", "cur"),
    "costi_totali": ("Costi totali", "cur"),
    "equity_finale": ("Equity finale", "cur"),
}


def format_table(metriche: dict[str, dict[str, float]]) -> str:
    """Tabella testuale con una colonna per serie e una riga per metrica."""
    nomi = list(metriche)
    larghezza = max([len(n) for n in nomi] + [12])
    righe = ["Metrica".ljust(22) + "".join(n.rjust(larghezza + 2) for n in nomi)]
    righe.append("-" * len(righe[0]))
    in_eccesso = any(m.get("excess", 0.0) > 0.0 for m in metriche.values())
    for chiave in ETICHETTE:
        etichetta, formato = metric_label(chiave, in_eccesso)
        valori = "".join(
            format_metric(metriche[n].get(chiave, 0.0), formato).rjust(larghezza + 2) for n in nomi
        )
        righe.append(etichetta.ljust(22) + valori)
    return "\n".join(righe)


def metric_label(chiave: str, in_eccesso: bool = False) -> tuple[str, str]:
    """Etichetta e formato di una metrica; lo Sharpe dichiara se e' in eccesso sul monetario."""
    etichetta, formato = ETICHETTE[chiave]
    if chiave == "sharpe" and in_eccesso:
        etichetta = "Sharpe (excess)"
    return etichetta, formato


def format_metric(valore: float, formato: str) -> str:
    """Rende leggibile un singolo valore secondo il tipo di metrica."""
    if formato == "pct":
        return f"{valore * 100:.2f}%"
    if formato == "int":
        return f"{int(valore)}"
    if formato == "cur":
        return f"{valore:,.0f}"
    return f"{valore:.2f}"


def compare(
    metrics_strategy: dict[str, float],
    metrics_benchmark: dict[str, float],
    labels: tuple[str, str] = ("strategia", "benchmark"),
) -> str:
    """Stampa e restituisce la tabella di confronto fra strategia e benchmark."""
    tabella = format_table({labels[0]: metrics_strategy, labels[1]: metrics_benchmark})
    print(tabella)
    return tabella
