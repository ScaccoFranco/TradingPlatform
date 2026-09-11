"""Analisi in-sample: momentum contro SPY e 60/40, piu' sensitivita' sui parametri."""

from __future__ import annotations

from quant.research.common import (
    CONFIG,
    REPORT,
    SIMBOLI,
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
    risultati = confronto(INIZIO, FINE)
    print(f"In-sample {INIZIO} -> {FINE}")
    print(tabella(risultati))
    print(riepilogo_costi(risultati))

    grafico = salva_grafico(risultati, REPORT / "insample_equity.png", f"In-sample {INIZIO} - {FINE}")
    print(f"grafico salvato in {grafico}")

    riscaldati = confronto(INIZIO_RISCALDATO, FINE, warmup_start=INIZIO)
    print(f"\nStesso confronto da {INIZIO_RISCALDATO}, con il {INIZIO[:4]} usato solo come storico")
    print(tabella(riscaldati))

    sensitivity = parameter_sensitivity(momentum_factory, GRIGLIA, SIMBOLI, INIZIO, FINE, config=CONFIG)
    REPORT.mkdir(parents=True, exist_ok=True)
    csv = REPORT / "sensitivity.csv"
    sensitivity.to_csv(csv, index=False)
    print(f"\nsensitivity su {len(sensitivity)} combinazioni salvata in {csv}")
    print(
        sensitivity.sort_values("sharpe", ascending=False)
        .head(5)[["lookback_months", "top_n", "trend_sma_days", "cagr", "sharpe", "max_drawdown"]]
        .to_string(index=False)
    )
    print(
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
    print(f"\nDeflated Sharpe della configurazione di default su {n_prove} prove: {dsr:.3f}")


if __name__ == "__main__":
    main()
