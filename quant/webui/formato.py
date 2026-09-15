"""Come si scrivono numeri, istanti ed etichette nelle pagine: formattazione, nessun calcolo."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime

from quant.analysis import format_metric, metric_label
from quant.logreader import KILL_SWITCH_ATTIVATO, MISMATCH, POSIZIONE_SENZA_BARRE, RUN_FALLITA
from quant.risk import RiskReason
from quant.state import ESEGUITO, STATI_CONCLUSI
from quant.webui.read import FUSO_BORSA

METRICHE = ("cagr", "volatilita", "sharpe", "sortino", "max_drawdown", "n_trade", "commissioni", "slippage")
DI_ESECUZIONE = frozenset({"n_trade", "commissioni", "slippage"})
DESCRIZIONE_MOTIVI: dict[str, str] = {
    RiskReason.OK: "approvato senza modifiche",
    RiskReason.KILL_SWITCH: "kill switch attivo: ogni ordine viene rifiutato",
    RiskReason.NOT_WHITELISTED: "simbolo fuori dall'universo ammesso",
    RiskReason.MAX_ORDERS_PER_DAY: "raggiunto il numero massimo di ordini della giornata",
    RiskReason.MAX_NOTIONAL_PER_ORDER: "controvalore oltre il massimo per singolo ordine",
    RiskReason.MAX_WEIGHT_PER_SYMBOL: "peso del simbolo oltre il massimo in portafoglio",
    RiskReason.MAX_GROSS_EXPOSURE: "esposizione lorda oltre il limite",
    RiskReason.MAX_DRAWDOWN: "drawdown oltre la soglia: niente rischio in piu'",
    RiskReason.NO_PRICE: "nessun prezzo, oppure equity non positiva",
    RiskReason.ZERO_QUANTITY: "dopo i limiti la quantita' si annulla",
}
DESCRIZIONE_EVENTI = {
    RUN_FALLITA: "esecuzione finita con un'eccezione",
    KILL_SWITCH_ATTIVATO: "kill switch attivato",
    MISMATCH: "posizioni attese diverse da quelle del broker",
    POSIZIONE_SENZA_BARRE: "posizione aperta senza barre recenti",
}


@dataclass(frozen=True, slots=True)
class RigaMetrica:
    """Riga della tabella delle metriche: etichetta e un valore gia' scritto per colonna."""

    etichetta: str
    valori: tuple[str, ...]


def valuta(valore: float | None) -> str:
    """Importo con separatore delle migliaia e due decimali, come in `scripts/status.py`."""
    return "-" if valore is None else f"{valore:,.2f}"


def bps(valore: float) -> str:
    """Scarto in punti base con il segno esplicito."""
    return f"{valore:+.1f} bps"


def bps_opz(valore: float | None) -> str:
    """Scarto in punti base in una cella, trattino se non misurabile."""
    return "-" if valore is None else f"{valore:+.1f}"


def momento_borsa(momento: datetime | None) -> str:
    """Istante nell'ora di New York, quella delle sedute e dello scheduler."""
    if momento is None:
        return "-"
    if momento.tzinfo is None:
        return momento.strftime("%Y-%m-%d %H:%M")
    return momento.astimezone(FUSO_BORSA).strftime("%Y-%m-%d %H:%M ET")


def timestamp_testo(testo: str) -> str:
    """Timestamp ISO salvato nello StateStore, ridotto a data e minuti."""
    return testo[:16].replace("T", " ")


def classe_stato(status: str) -> str:
    """Stato di un ordine in tre famiglie: eseguito, chiuso senza esecuzione, ancora aperto."""
    if status == ESEGUITO:
        return "eseguito"
    return "chiuso" if status in STATI_CONCLUSI else "aperto"


def descrivi_motivo(motivo: str) -> str:
    """Cosa vuol dire un motivo di RiskReason, in una riga."""
    return DESCRIZIONE_MOTIVI.get(motivo, "motivo non documentato in RiskReason")


def descrivi_evento(evento: str) -> str:
    """Cosa vuol dire un evento anomalo del log, in una riga."""
    return DESCRIZIONE_EVENTI.get(evento, evento)


def dimensione(byte: int) -> str:
    """Dimensione di un file in unita' leggibili."""
    if byte < 1024:
        return f"{byte} B"
    if byte < 1024**2:
        return f"{byte / 1024:.1f} kB"
    return f"{byte / 1024**2:.1f} MB"


def tabella_metriche(
    metriche: Mapping[str, Mapping[str, float]], senza_esecuzione: Collection[str] = ()
) -> tuple[RigaMetrica, ...]:
    """Metriche di `compute_metrics` con le etichette e i formati di `quant.analysis`.

    Le colonne in `senza_esecuzione`, come un indice di mercato, non hanno trade ne' costi
    e mostrano un trattino; una colonna senza osservazioni lo mostra al posto delle
    metriche di rendimento, perche' uno zero sembrerebbe un risultato.
    """
    in_eccesso = any(m.get("excess", 0.0) > 0.0 for m in metriche.values())
    righe: list[RigaMetrica] = []
    for chiave in METRICHE:
        etichetta, formato = metric_label(chiave, in_eccesso)
        valori = tuple(
            _cella(valori, chiave, formato, nome in senza_esecuzione) for nome, valori in metriche.items()
        )
        righe.append(RigaMetrica(etichetta, valori))
    return tuple(righe)


def _cella(metriche: Mapping[str, float], chiave: str, formato: str, senza_esecuzione: bool) -> str:
    """Un valore della tabella, o un trattino dove il numero non avrebbe senso."""
    if chiave in DI_ESECUZIONE and senza_esecuzione:
        return "-"
    if chiave not in DI_ESECUZIONE and metriche.get("n_osservazioni", 0.0) == 0.0:
        return "-"
    return format_metric(metriche.get(chiave, 0.0), formato)
