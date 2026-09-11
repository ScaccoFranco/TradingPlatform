"""Validazione out-of-sample: parametri di DEFAULT dal 2019 a oggi, un solo lancio."""

from __future__ import annotations

from datetime import date

from quant.research.common import REPORT, confronto, quiet_logging, riepilogo_costi, salva_grafico, tabella

INIZIO = "2019-01-01"
FINE = date.today().isoformat()
RISCALDAMENTO = "2017-01-01"  # solo storico: le metriche partono comunque dal 2019


def main() -> None:
    """Stesso confronto a tre dell'analisi in-sample, sul periodo mai usato in sviluppo."""
    quiet_logging()
    risultati = confronto(INIZIO, FINE, warmup_start=RISCALDAMENTO)
    print(f"Out-of-sample {INIZIO} -> {FINE}, parametri di default, storico da {RISCALDAMENTO}")
    print(tabella(risultati))
    print(riepilogo_costi(risultati))
    grafico = salva_grafico(risultati, REPORT / "oos_equity.png", f"Out-of-sample {INIZIO} - {FINE}")
    print(f"grafico salvato in {grafico}")


if __name__ == "__main__":
    main()
