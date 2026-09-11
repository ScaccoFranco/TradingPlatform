"""Fotografia dello stato live: posizioni, cassa, equity e ultimi ordini."""

from __future__ import annotations

import sys
from pathlib import Path

from quant.risk import KillSwitch
from quant.state import PERCORSO_DB, StateStore

GIORNI = 30
ORDINI = 10


def stampa_posizioni(store: StateStore) -> None:
    """Posizioni attese secondo lo stato salvato."""
    posizioni = store.load_positions()
    print("\nPosizioni attese")
    if not posizioni:
        print("  nessuna")
        return
    for symbol, quantita in sorted(posizioni.items()):
        print(f"  {symbol:<6} {quantita:>10}")


def stampa_equity(store: StateStore, giorni: int = GIORNI) -> None:
    """Cassa ed equity delle ultime giornate registrate."""
    serie = store.load_equity(giorni)
    print(f"\nEquity degli ultimi {giorni} giorni")
    if not serie:
        print("  nessuna rilevazione")
        return
    for giorno, cash, valore_posizioni, totale in serie:
        print(
            f"  {giorno}  cash {cash:>12,.2f}  "
            f"posizioni {valore_posizioni:>12,.2f}  totale {totale:>12,.2f}"
        )
    primo, ultimo = serie[0][3], serie[-1][3]
    if primo > 0:
        print(f"  variazione sul periodo: {(ultimo / primo - 1) * 100:+.2f}%")


def stampa_ordini(store: StateStore, quanti: int = ORDINI) -> None:
    """Ultimi ordini inviati e relativo stato."""
    ordini = store.recent_orders(quanti)
    print(f"\nUltimi {quanti} ordini")
    if not ordini:
        print("  nessun ordine")
        return
    for ordine in ordini:
        prezzo = f"{ordine.limit_price:.2f}" if ordine.limit_price is not None else "market"
        print(
            f"  {ordine.timestamp[:19]}  {ordine.symbol:<6} {ordine.direction:<4} "
            f"{ordine.quantity:>8}  {prezzo:>8}  {ordine.status:<10} {ordine.client_order_id}"
        )


def main(percorso: str | Path = PERCORSO_DB) -> None:
    """Stampa il riepilogo completo."""
    interruttore = KillSwitch()
    print(f"Stato live da {percorso}")
    if interruttore.is_active():
        print(f"KILL SWITCH ATTIVO: {interruttore.reason()}")
    if not Path(percorso).exists():
        print("database non ancora creato: nessuna esecuzione live registrata")
        return
    store = StateStore(percorso)
    stampa_posizioni(store)
    stampa_equity(store)
    stampa_ordini(store)
    store.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else PERCORSO_DB)
