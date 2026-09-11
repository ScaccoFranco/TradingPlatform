"""Report settimanale: quanto il live si e' scostato dal backtest, e dove."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from quant.logging import get_logger
from quant.logreader import (
    PERCORSO_LOG,
    event_day,
    failures,
    mismatches,
    read_events,
    risk_decisions,
    run_days,
)
from quant.reconcile_report import (
    FillComparison,
    ReconcileSummary,
    compare,
    live_equity_series,
    real_fills,
    reference_opens,
    summarize,
    theoretical_fills,
)
from quant.shadow import ShadowResult
from quant.state import StateStore
from quant.validation import PERCORSO_DATI

logger = get_logger("weekly")

REPORT = Path("reports")
SOGLIA_TRACKING_ERROR_BPS = 50.0
SOGLIA_RAPPORTO_SLIPPAGE = 2.0
BENCHMARK = "SPY"
BASE = 100.0


@dataclass(slots=True)
class WeeklyReport:
    """Report generato: percorso, testo, righe di sintesi per l'alert."""

    path: Path
    markdown: str
    summary_lines: list[str] = field(default_factory=list)
    attenzione: bool = False
    chart: Path | None = None

    @property
    def alert_text(self) -> str:
        """Tre righe piu' il percorso, quanto basta a un messaggio Telegram."""
        return "\n".join([*self.summary_lines[:3], str(self.path)])


def week_bounds(giorno: date) -> tuple[date, date]:
    """Lunedi' e domenica della settimana che contiene la data."""
    lunedi = giorno - timedelta(days=giorno.weekday())
    return lunedi, lunedi + timedelta(days=6)


def benchmark_series(path: str | Path = PERCORSO_DATI, symbol: str = BENCHMARK) -> pd.Series:
    """Serie del benchmark sui prezzi rettificati, per un paragone di mercato."""
    file = Path(path) / f"{symbol}.parquet"
    if not file.exists():
        return pd.Series(dtype="float64")
    barre = pd.read_parquet(file, columns=["adj_close"])
    barre.index = pd.DatetimeIndex(barre.index).normalize()
    return barre["adj_close"]


def rebase(serie: pd.Series, base: float = BASE) -> pd.Series:
    """Riporta una serie a base comune per confrontarne la forma, non il livello."""
    pulita = serie.dropna()
    if pulita.empty or pulita.iloc[0] <= 0:
        return pulita
    return pulita / pulita.iloc[0] * base


def salva_grafico(
    live: pd.Series,
    shadow: pd.Series,
    benchmark: pd.Series,
    destinazione: Path,
    titolo: str,
) -> Path | None:
    """Tre curve in scala logaritmica, tutte a base comune."""
    serie = {"live": rebase(live), "shadow": rebase(shadow), BENCHMARK: rebase(benchmark)}
    serie = {nome: s for nome, s in serie.items() if len(s) > 1}
    if not serie:
        return None
    destinazione.parent.mkdir(parents=True, exist_ok=True)
    figura, asse = plt.subplots(figsize=(11, 6))
    for nome, valori in serie.items():
        asse.plot(valori.index, valori.values, label=nome, linewidth=1.2)
    asse.set_yscale("log")
    asse.set_title(titolo)
    asse.set_ylabel("base 100 (scala log)")
    asse.grid(True, which="both", alpha=0.25)
    asse.legend()
    figura.tight_layout()
    figura.savefig(destinazione, dpi=130)
    plt.close(figura)
    return destinazione


def giorni_di_borsa(inizio: date, fine: date, path: str | Path = PERCORSO_DATI) -> list[date]:
    """Giornate di borsa nel periodo, prese dal calendario del benchmark."""
    serie = benchmark_series(path)
    if serie.empty:
        return [
            g
            for g in pd.date_range(inizio, fine).date
            if g.weekday() < 5
        ]
    finestra = serie.loc[str(inizio) : str(fine)]
    return [d.date() for d in finestra.index]


def _tabella_fill(confronti: Sequence[FillComparison]) -> str:
    """Tabella markdown degli eseguiti della settimana."""
    if not confronti:
        return "Nessun eseguito in settimana.\n"
    righe = [
        "| Giorno | Simbolo | Stato | Qta live | Qta shadow | Prezzo live | Prezzo shadow | Scarto bps |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in confronti:
        righe.append(
            f"| {c.day} | {c.symbol} | {c.status} | {_num(c.real.quantity if c.real else None)} | "
            f"{_num(c.theoretical.quantity if c.theoretical else None)} | "
            f"{_num(c.real.price if c.real else None, 2)} | "
            f"{_num(c.theoretical.price if c.theoretical else None, 2)} | {_num(c.diff_bps, 1)} |"
        )
    return "\n".join(righe) + "\n"


def _num(valore: object, decimali: int = 0) -> str:
    """Numero formattato, trattino se assente."""
    if valore is None or (isinstance(valore, float) and pd.isna(valore)):
        return "-"
    if isinstance(valore, float):
        return f"{valore:.{decimali}f}"
    return str(valore)


def build_weekly_report(
    store: StateStore,
    shadow: ShadowResult,
    riferimento: date | None = None,
    directory: str | Path = REPORT,
    data_path: str | Path = PERCORSO_DATI,
    log_path: str | Path = PERCORSO_LOG,
    soglia_tracking_error_bps: float = SOGLIA_TRACKING_ERROR_BPS,
    soglia_rapporto_slippage: float = SOGLIA_RAPPORTO_SLIPPAGE,
) -> WeeklyReport:
    """Genera il markdown della settimana e il grafico che lo accompagna.

    Legge soltanto: StateStore, Parquet, log e risultato dello shadow. Non scrive
    nulla oltre ai file di report.
    """
    riferimento = riferimento or date.today()
    inizio, fine = week_bounds(riferimento)
    cartella = Path(directory)
    cartella.mkdir(parents=True, exist_ok=True)

    live = live_equity_series(store)
    ombra = shadow.equity_series()
    reali = real_fills(store)
    teorici = theoretical_fills(shadow)
    aperture = reference_opens({f.symbol for f in [*reali, *teorici]}, data_path)

    confronti = compare(reali, teorici, aperture)
    della_settimana = [c for c in confronti if inizio <= c.day <= fine]
    sommario = summarize(confronti, live_equity=live, shadow_equity=ombra, generated_at=riferimento)

    eventi = read_events(log_path, since=inizio, until=fine)
    decisioni = risk_decisions(eventi)
    disallineamenti = mismatches(eventi)
    guasti = failures(eventi)

    borsa = giorni_di_borsa(inizio, fine, data_path)
    girati = {g for g in live.index.date if inizio <= g <= fine} | run_days(eventi)
    saltati = [g for g in borsa if g not in girati]

    grafico = salva_grafico(
        live,
        ombra,
        benchmark_series(data_path),
        cartella / f"weekly_{riferimento.isocalendar().year}-{riferimento.isocalendar().week:02d}.png",
        f"Live, shadow e {BENCHMARK} dal {shadow.start}",
    )

    avvisi = _avvisi(
        sommario, saltati, disallineamenti, guasti, soglia_tracking_error_bps, soglia_rapporto_slippage
    )
    sintesi = [
        f"Tracking error cumulato {sommario.tracking_error_cumulative_bps:+.1f} bps "
        f"su {sommario.days_compared} giorni.",
        f"Slippage reale {sommario.slippage_real_mean_bps:.1f} bps contro "
        f"{sommario.slippage_theoretical_mean_bps:.1f} bps simulati.",
        f"Eseguiti appaiati {sommario.n_matched}, solo live {sommario.n_solo_live}, "
        f"solo shadow {sommario.n_solo_shadow}, giorni saltati {len(saltati)}.",
    ]

    testo = _componi(
        riferimento=riferimento,
        inizio=inizio,
        fine=fine,
        shadow=shadow,
        sommario=sommario,
        sintesi=sintesi,
        avvisi=avvisi,
        confronti=della_settimana,
        decisioni=decisioni,
        disallineamenti=disallineamenti,
        guasti=guasti,
        saltati=saltati,
        grafico=grafico,
    )

    anno, settimana, _ = riferimento.isocalendar()
    destinazione = cartella / f"weekly_{anno}-{settimana:02d}.md"
    destinazione.write_text(testo, encoding="utf-8")
    logger.info(
        "report_settimanale",
        path=str(destinazione),
        attenzione=bool(avvisi),
        tracking_error_bps=round(sommario.tracking_error_cumulative_bps, 2),
    )
    return WeeklyReport(destinazione, testo, sintesi, bool(avvisi), grafico)


def _avvisi(
    sommario: ReconcileSummary,
    saltati: Sequence[date],
    disallineamenti: Sequence[dict[str, Any]],
    guasti: Sequence[dict[str, Any]],
    soglia_te: float,
    soglia_slippage: float,
) -> list[str]:
    """Motivi per cui il report deve aprire con un blocco di attenzione."""
    avvisi: list[str] = []
    if abs(sommario.tracking_error_cumulative_bps) > soglia_te:
        avvisi.append(
            f"Tracking error cumulato {sommario.tracking_error_cumulative_bps:+.1f} bps, "
            f"oltre la soglia di {soglia_te:.0f} bps. Il live si sta scostando dal backtest: "
            "guardare la tabella degli eseguiti e la colonna scarto bps."
        )
    rapporto = sommario.slippage_ratio
    if rapporto is not None and rapporto > soglia_slippage:
        avvisi.append(
            f"Slippage reale {sommario.slippage_real_mean_bps:.1f} bps contro "
            f"{sommario.slippage_theoretical_mean_bps:.1f} bps simulati, {rapporto:.1f} volte tanto. "
            "I costi del backtest sottostimano quelli veri: vedere la tabella per simbolo."
        )
    if sommario.n_solo_live or sommario.n_solo_shadow:
        avvisi.append(
            f"Eseguiti presenti da un lato solo: {sommario.n_solo_live} solo live, "
            f"{sommario.n_solo_shadow} solo shadow. Un ordine non eseguito o un fill non registrato."
        )
    if disallineamenti:
        avvisi.append(f"{len(disallineamenti)} RECONCILIATION_MISMATCH nei log della settimana.")
    if guasti:
        avvisi.append(f"{len(guasti)} fra eccezioni e attivazioni del kill switch nei log della settimana.")
    if saltati:
        avvisi.append(f"Il runner non ha girato in {len(saltati)} giornate di borsa: {_elenco(saltati)}.")
    return avvisi


def _elenco(giorni: Sequence[date]) -> str:
    """Elenco compatto di date."""
    return ", ".join(str(g) for g in giorni[:8]) + (" ..." if len(giorni) > 8 else "")


def _componi(**dati: Any) -> str:
    """Assembla il markdown del report."""
    sommario = dati["sommario"]
    anno, settimana, _ = dati["riferimento"].isocalendar()
    parti: list[str] = []

    if dati["avvisi"]:
        parti.append("> **ATTENZIONE**\n>")
        for avviso in dati["avvisi"]:
            parti.append(f"> - {avviso}")
        parti.append("")

    parti.append(f"# Settimana {anno}-{settimana:02d} ({dati['inizio']} - {dati['fine']})\n")
    parti.append("## Sintesi\n")
    parti.extend(f"- {riga}" for riga in dati["sintesi"])
    parti.append("")

    parti.append("## Scostamento fra live e shadow\n")
    parti.append("| Metrica | Valore |")
    parti.append("|---|---|")
    parti.append(f"| Periodo confrontato | {dati['shadow'].start} - {dati['shadow'].end} |")
    parti.append(f"| Giorni confrontati | {sommario.days_compared} |")
    parti.append(f"| Tracking error giornaliero | {sommario.tracking_error_daily_bps:.1f} bps |")
    parti.append(f"| Tracking error cumulato | {sommario.tracking_error_cumulative_bps:+.1f} bps |")
    parti.append(f"| Slippage reale medio | {sommario.slippage_real_mean_bps:.1f} bps |")
    parti.append(f"| Slippage simulato medio | {sommario.slippage_theoretical_mean_bps:.1f} bps |")
    parti.append(f"| Commissioni reali | {sommario.cost_real:,.2f} |")
    parti.append(f"| Commissioni simulate | {sommario.cost_theoretical:,.2f} |")
    parti.append("")

    if dati["grafico"] is not None:
        parti.append(f"![equity]({dati['grafico'].name})\n")

    parti.append("## Eseguiti della settimana\n")
    parti.append(_tabella_fill(dati["confronti"]))

    if not sommario.per_symbol.empty:
        parti.append("## Slippage per simbolo (intero periodo)\n")
        parti.append(_tabella_dataframe(sommario.per_symbol.round(2)))

    parti.append("## Decisioni del gestore del rischio\n")
    parti.append(_tabella_decisioni(dati["decisioni"]))

    parti.append("## Anomalie\n")
    parti.append(f"- RECONCILIATION_MISMATCH: {len(dati['disallineamenti'])}")
    parti.append(f"- Eccezioni e kill switch: {len(dati['guasti'])}")
    parti.append(f"- Giornate di borsa senza esecuzione: {len(dati['saltati'])}")
    if dati["saltati"]:
        parti.append(f"  - {_elenco(dati['saltati'])}")
    parti.append("")
    return "\n".join(parti)


def _tabella_dataframe(frame: pd.DataFrame) -> str:
    """Tabella markdown da un DataFrame, senza dipendenze aggiuntive."""
    intestazione = "| " + " | ".join(str(c) for c in frame.columns) + " |"
    separatore = "|" + "|".join("---" for _ in frame.columns) + "|"
    righe = [
        "| " + " | ".join(_num(v, 2) if isinstance(v, float) else str(v) for v in riga) + " |"
        for riga in frame.itertuples(index=False, name=None)
    ]
    return "\n".join([intestazione, separatore, *righe]) + "\n"


def _tabella_decisioni(decisioni: Sequence[dict[str, Any]]) -> str:
    """Tabella markdown delle decisioni di rischio lette dai log."""
    if not decisioni:
        return "Nessuna decisione registrata nei log della settimana.\n"
    righe = ["| Giorno | Evento | Simbolo | Richiesta | Finale | Motivo |", "|---|---|---|---|---|---|"]
    for evento in decisioni:
        righe.append(
            f"| {event_day(evento)} | {evento.get('event')} | {evento.get('symbol', '-')} | "
            f"{evento.get('quantity_original', '-')} | {evento.get('quantity_final', '-')} | "
            f"{evento.get('reason', '-')} |"
        )
    return "\n".join(righe) + "\n"


def generate_weekly_report(
    giorno: date | None = None,
    avvio: str | None = None,
    db: str | Path | None = None,
    dati: str | Path = PERCORSO_DATI,
    log: str | Path = PERCORSO_LOG,
    directory: str | Path = REPORT,
    soglia: float = SOGLIA_TRACKING_ERROR_BPS,
) -> WeeklyReport:
    """Rigioca lo shadow dall'avvio del live e costruisce il report della settimana.

    Usa la strategia e l'universo dichiarati in `quant.live.deployment`, gli stessi
    che gira lo scheduler. Apre lo StateStore solo per leggerlo.
    """
    from quant.live.deployment import AVVIO_LIVE, SIMBOLI, strategy_factory
    from quant.shadow import ShadowBacktest
    from quant.state import PERCORSO_DB

    giorno = giorno or date.today()
    ombra = ShadowBacktest(strategy_factory, avvio or AVVIO_LIVE, SIMBOLI, path=dati).run(end=giorno)
    store = StateStore(db or PERCORSO_DB)
    try:
        return build_weekly_report(
            store,
            ombra,
            riferimento=giorno,
            directory=directory,
            data_path=dati,
            log_path=log,
            soglia_tracking_error_bps=soglia,
        )
    finally:
        store.close()
