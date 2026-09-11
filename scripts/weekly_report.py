"""Genera il report settimanale di confronto fra live e shadow backtest.

Sola lettura: apre lo StateStore in lettura, i Parquet e i log, scrive solo i report.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from quant.live.deployment import AVVIO_LIVE
from quant.logreader import PERCORSO_LOG
from quant.state import PERCORSO_DB
from quant.weekly import SOGLIA_TRACKING_ERROR_BPS, generate_weekly_report


def main(argv: list[str] | None = None) -> None:
    """Genera il report e ne stampa la sintesi."""
    parser = argparse.ArgumentParser(description="Report settimanale live contro shadow")
    parser.add_argument("--giorno", default=None, help="data di riferimento, default oggi")
    parser.add_argument("--avvio", default=AVVIO_LIVE, help="giorno di avvio del live")
    parser.add_argument("--db", default=str(PERCORSO_DB), help="percorso dello StateStore")
    parser.add_argument("--log", default=str(PERCORSO_LOG), help="percorso del log JSON")
    parser.add_argument("--soglia", type=float, default=SOGLIA_TRACKING_ERROR_BPS, help="soglia in bps")
    argomenti = parser.parse_args(sys.argv[1:] if argv is None else argv)

    report = generate_weekly_report(
        giorno=date.fromisoformat(argomenti.giorno) if argomenti.giorno else None,
        avvio=argomenti.avvio,
        db=argomenti.db,
        log=argomenti.log,
        soglia=argomenti.soglia,
    )
    print(report.alert_text)
    if report.attenzione:
        print("\nIl report si apre con un blocco ATTENZIONE.")


if __name__ == "__main__":
    main()
