"""Analisi in-sample: momentum contro SPY e 60/40, piu' sensitivita' sui parametri."""

from __future__ import annotations

from quant.research.common import (
    CONFIG,
    REPORT,
    SIMBOLI,
    Resoconto,
    confronto,
    momentum_factory,
    quiet_logging,
    riepilogo_costi,
    salva_grafico,
    tabella,
)
from quant.validation import deflated_sharpe, parameter_combinations, parameter_sensitivity

INIZIO = "2005-01-01"
FINE = "2018-12-31"
INIZIO_RISCALDATO = "2006-01-01"  # il momentum a 12 mesi non ha storico prima
GRIGLIA = {
    "lookback_months": [3, 6, 9, 12],
    "top_n": [2, 3, 4],
    "trend_sma_days": [100, 200],
}
GIORNI_ANNO = 252


def main() -> None:
    """Esegue il confronto a tre, la griglia di sensitivita' e il Deflated Sharpe."""
    quiet_logging()
    resoconto = Resoconto("insample")
    risultati = confronto(INIZIO, FINE)
    resoconto.registra(risultati)
    resoconto.scrivi(f"# In-sample {INIZIO} -> {FINE}\n")
    resoconto.tabella(tabella(risultati))
    resoconto.scrivi(riepilogo_costi(risultati))

    grafico = salva_grafico(risultati, REPORT / "insample_equity.png", f"In-sample {INIZIO} - {FINE}")
    resoconto.scrivi(f"\ngrafico salvato in {grafico}")

    riscaldati = confronto(INIZIO_RISCALDATO, FINE, warmup_start=INIZIO)
    resoconto.registra(riscaldati, prefisso="riscaldato ")
    resoconto.scrivi(
        f"\nStesso confronto da {INIZIO_RISCALDATO}, con il {INIZIO[:4]} usato solo come storico\n"
    )
    resoconto.tabella(tabella(riscaldati))

    sensitivity = parameter_sensitivity(momentum_factory, GRIGLIA, SIMBOLI, INIZIO, FINE, config=CONFIG)
    REPORT.mkdir(parents=True, exist_ok=True)
    csv = REPORT / "sensitivity.csv"
    sensitivity.to_csv(csv, index=False)
    resoconto.scrivi(
        f"\nsensitivity su {len(sensitivity)} combinazioni salvata in {csv}, "
        "con l'impronta di provenienza di ogni riga\n"
    )
    resoconto.tabella(
        sensitivity.sort_values("sharpe", ascending=False)
        .head(5)[["lookback_months", "top_n", "trend_sma_days", "cagr", "sharpe", "max_drawdown"]]
        .to_string(index=False)
    )
    resoconto.scrivi(
        f"sharpe: min {sensitivity['sharpe'].min():.2f} "
        f"mediana {sensitivity['sharpe'].median():.2f} max {sensitivity['sharpe'].max():.2f}"
    )

    metriche = risultati["momentum"]["metrics"]
    n_prove = len(parameter_combinations(GRIGLIA))
    dsr = deflated_sharpe(
        sharpe_observed=metriche["sharpe"] / GIORNI_ANNO**0.5,
        n_trials=n_prove,
        n_obs=int(metriche["n_osservazioni"]),
        skew=metriche["skew"],
        kurt=metriche["kurtosis"],
    )
    resoconto.scrivi(f"\nDeflated Sharpe della configurazione di default su {n_prove} prove: {dsr:.3f}")
    resoconto.salva()


if __name__ == "__main__":
    main()
