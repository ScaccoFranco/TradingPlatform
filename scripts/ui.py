"""Dashboard locale di sola lettura: stato live, performance, ordini, rischio e report.

Ascolta solo su 127.0.0.1 o localhost; qualunque altro host fa uscire con codice 2.
Guida in docs/ui.md.
"""

from __future__ import annotations

import argparse
import sys

from quant.webui.avvio import HOST_PREDEFINITO, PORTA_PREDEFINITA, avvia


def main(argv: list[str] | None = None) -> int:
    """Legge le opzioni e avvia la dashboard."""
    parser = argparse.ArgumentParser(description="Dashboard locale di sola lettura")
    parser.add_argument("--host", default=HOST_PREDEFINITO, help="indirizzo di ascolto, solo loopback")
    parser.add_argument("--port", type=int, default=PORTA_PREDEFINITA, help="porta, default 8000")
    parser.add_argument("--db", default=None, help="StateStore del live, default data/live.db")
    parser.add_argument("--data", default=None, help="cartella dei Parquet, default data/parquet")
    parser.add_argument("--reports", default=None, help="cartella dei report, default reports")
    parser.add_argument("--log", default=None, help="log JSON, default QUANT_LOG_FILE o logs/live.jsonl")
    a = parser.parse_args(sys.argv[1:] if argv is None else argv)
    return avvia(a.host, a.port, db=a.db, dati=a.data, report=a.reports, log=a.log)


if __name__ == "__main__":
    raise SystemExit(main())
