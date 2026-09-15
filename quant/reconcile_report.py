"""Confronto fra gli eseguiti reali e quelli teorici dello shadow backtest.

Tutto in sola lettura: legge lo StateStore, i Parquet e il risultato dello shadow,
scrive solo file di report. Non tocca lo stato live e non manda ordini.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from quant.events import FillEvent
from quant.logging import get_logger
from quant.shadow import ShadowResult
from quant.state import StateStore

logger = get_logger("reconcile")

REPORT = Path("reports")
STORICO = "reconcile_history.csv"
BPS = 10_000.0

SOLO_LIVE = "solo_live"
SOLO_SHADOW = "solo_shadow"
APPAIATO = "appaiato"


@dataclass(frozen=True, slots=True)
class FillRecord:
    """Eseguito normalizzato, da qualunque lato arrivi."""

    timestamp: datetime
    symbol: str
    direction: str
    quantity: int
    price: float
    commission: float
    signal_timestamp: datetime | None = None

    @property
    def day(self) -> date:
        """Giornata di esecuzione."""
        giorno: date = pd.Timestamp(self.timestamp).date()
        return giorno

    @property
    def delay_days(self) -> float | None:
        """Giorni fra il segnale e l'eseguito, None se il segnale non e' noto."""
        if self.signal_timestamp is None:
            return None
        secondi: float = (pd.Timestamp(self.timestamp) - pd.Timestamp(self.signal_timestamp)).total_seconds()
        return secondi / 86_400.0


@dataclass(frozen=True, slots=True)
class FillComparison:
    """Riga del report: un eseguito visto dai due lati."""

    day: date
    symbol: str
    status: str
    real: FillRecord | None
    theoretical: FillRecord | None
    reference_open: float | None

    @property
    def diff_bps(self) -> float | None:
        """Scarto di prezzo fra reale e teorico, positivo se il reale e' peggiore."""
        if self.real is None or self.theoretical is None or self.theoretical.price <= 0:
            return None
        return _segno(self.real.direction) * (self.real.price / self.theoretical.price - 1.0) * BPS

    @property
    def quantity_diff(self) -> int | None:
        """Differenza di quantita' fra reale e teorico."""
        if self.real is None or self.theoretical is None:
            return None
        return self.real.quantity - self.theoretical.quantity

    @property
    def slippage_real_bps(self) -> float | None:
        """Scostamento del prezzo reale dall'apertura della giornata."""
        return _slippage(self.real, self.reference_open)

    @property
    def slippage_theoretical_bps(self) -> float | None:
        """Scostamento del prezzo teorico dalla stessa apertura."""
        return _slippage(self.theoretical, self.reference_open)

    def as_row(self) -> dict[str, object]:
        """Riga pronta per il CSV."""
        presente = self.real or self.theoretical
        return {
            "day": self.day.isoformat(),
            "symbol": self.symbol,
            "status": self.status,
            "direction": presente.direction if presente is not None else "",
            "quantity_real": self.real.quantity if self.real else None,
            "quantity_theoretical": self.theoretical.quantity if self.theoretical else None,
            "quantity_diff": self.quantity_diff,
            "price_real": self.real.price if self.real else None,
            "price_theoretical": self.theoretical.price if self.theoretical else None,
            "diff_bps": self.diff_bps,
            "reference_open": self.reference_open,
            "slippage_real_bps": self.slippage_real_bps,
            "slippage_theoretical_bps": self.slippage_theoretical_bps,
            "commission_real": self.real.commission if self.real else None,
            "commission_theoretical": self.theoretical.commission if self.theoretical else None,
            "delay_days_real": self.real.delay_days if self.real else None,
            "delay_days_theoretical": self.theoretical.delay_days if self.theoretical else None,
        }


@dataclass(slots=True)
class ReconcileSummary:
    """Sintesi di una esecuzione del confronto."""

    generated_at: date
    n_matched: int = 0
    n_solo_live: int = 0
    n_solo_shadow: int = 0
    slippage_real_mean_bps: float = 0.0
    slippage_real_median_bps: float = 0.0
    slippage_theoretical_mean_bps: float = 0.0
    slippage_theoretical_median_bps: float = 0.0
    diff_mean_bps: float = 0.0
    cost_real: float = 0.0
    cost_theoretical: float = 0.0
    tracking_error_daily_bps: float = 0.0
    tracking_error_cumulative_bps: float = 0.0
    days_compared: int = 0
    per_symbol: pd.DataFrame = field(default_factory=pd.DataFrame)

    def as_row(self) -> dict[str, object]:
        """Riga per lo storico delle esecuzioni."""
        return {
            "generated_at": self.generated_at.isoformat(),
            "n_matched": self.n_matched,
            "n_solo_live": self.n_solo_live,
            "n_solo_shadow": self.n_solo_shadow,
            "slippage_real_mean_bps": round(self.slippage_real_mean_bps, 3),
            "slippage_theoretical_mean_bps": round(self.slippage_theoretical_mean_bps, 3),
            "diff_mean_bps": round(self.diff_mean_bps, 3),
            "cost_real": round(self.cost_real, 2),
            "cost_theoretical": round(self.cost_theoretical, 2),
            "tracking_error_daily_bps": round(self.tracking_error_daily_bps, 3),
            "tracking_error_cumulative_bps": round(self.tracking_error_cumulative_bps, 3),
            "days_compared": self.days_compared,
        }

    @property
    def slippage_ratio(self) -> float | None:
        """Quante volte lo slippage reale supera quello simulato."""
        if abs(self.slippage_theoretical_mean_bps) < 1e-9:
            return None
        return self.slippage_real_mean_bps / self.slippage_theoretical_mean_bps


def _segno(direction: str) -> float:
    """Rende positivo lo scarto quando l'esecuzione e' sfavorevole al trader."""
    return 1.0 if str(direction).upper().endswith("BUY") else -1.0


def _slippage(fill: FillRecord | None, riferimento: float | None) -> float | None:
    """Scostamento in bps dal prezzo di riferimento, positivo se sfavorevole."""
    if fill is None or riferimento is None or riferimento <= 0:
        return None
    return _segno(fill.direction) * (fill.price / riferimento - 1.0) * BPS


def real_fills(store: StateStore) -> list[FillRecord]:
    """Eseguiti reali dallo StateStore, con il timestamp dell'ordine che li ha generati."""
    righe = store.connection.execute(
        """SELECT f.*, o.timestamp AS order_timestamp
           FROM fills f LEFT JOIN orders o ON o.client_order_id = f.client_order_id
           ORDER BY f.timestamp"""
    ).fetchall()
    return [
        FillRecord(
            timestamp=datetime.fromisoformat(r["timestamp"]),
            symbol=r["symbol"],
            direction=r["direction"],
            quantity=int(r["quantity"]),
            price=float(r["fill_price"]),
            commission=float(r["commission"]),
            signal_timestamp=datetime.fromisoformat(r["order_timestamp"]) if r["order_timestamp"] else None,
        )
        for r in righe
    ]


def theoretical_fills(shadow: ShadowResult) -> list[FillRecord]:
    """Eseguiti teorici dallo shadow.

    Il segnale che li ha prodotti sta sulla barra precedente per costruzione: nel
    backtest un ordine deciso a T viene eseguito all'apertura di T+1. I timestamp
    teorici sono portati a mezzanotte perche' una barra daily non ha un orario:
    il ritardo teorico e' quindi esattamente una giornata di borsa, mentre quello
    reale conserva l'ora effettiva dell'eseguito.
    """
    calendario = sorted({pd.Timestamp(t).normalize() for t, _ in shadow.equity_curve})
    record: list[FillRecord] = []
    for fill in shadow.fills:
        momento = pd.Timestamp(fill.timestamp).normalize()
        posizione = int(pd.DatetimeIndex(calendario).searchsorted(momento, side="left"))
        precedente = calendario[posizione - 1] if posizione > 0 else None
        record.append(
            FillRecord(
                timestamp=momento.to_pydatetime(),
                symbol=fill.symbol,
                direction=str(fill.direction),
                quantity=int(fill.quantity),
                price=float(fill.fill_price),
                commission=float(fill.commission),
                signal_timestamp=precedente.to_pydatetime() if precedente is not None else None,
            )
        )
    return record


def reference_opens(symbols: Iterable[str], path: str | Path) -> dict[str, pd.Series]:
    """Aperture ufficiali per simbolo, usate come riferimento comune dei due lati."""
    aperture: dict[str, pd.Series] = {}
    for symbol in sorted(set(symbols)):
        file = Path(path) / f"{symbol}.parquet"
        if not file.exists():
            continue
        barre = pd.read_parquet(file, columns=["open"])
        barre.index = pd.DatetimeIndex(barre.index).normalize()
        aperture[symbol] = barre["open"]
    return aperture


def compare(
    reali: Sequence[FillRecord],
    teorici: Sequence[FillRecord],
    aperture: dict[str, pd.Series] | None = None,
) -> list[FillComparison]:
    """Appaia gli eseguiti per giornata e simbolo, segnalando chi manca da un lato."""
    aperture = aperture or {}
    indice_reali = {(f.day, f.symbol): f for f in reali}
    indice_teorici = {(f.day, f.symbol): f for f in teorici}

    confronti: list[FillComparison] = []
    for chiave in sorted(set(indice_reali) | set(indice_teorici)):
        giorno, symbol = chiave
        reale = indice_reali.get(chiave)
        teorico = indice_teorici.get(chiave)
        if reale is not None and teorico is not None:
            stato = APPAIATO
        elif reale is not None:
            stato = SOLO_LIVE
        else:
            stato = SOLO_SHADOW
        confronti.append(
            FillComparison(
                day=giorno,
                symbol=symbol,
                status=stato,
                real=reale,
                theoretical=teorico,
                reference_open=_apertura(aperture, symbol, giorno),
            )
        )
    return confronti


def _apertura(aperture: dict[str, pd.Series], symbol: str, giorno: date) -> float | None:
    """Apertura ufficiale di quel simbolo in quella giornata, se disponibile."""
    serie = aperture.get(symbol)
    if serie is None:
        return None
    momento = pd.Timestamp(giorno)
    if momento not in serie.index:
        return None
    return float(serie.loc[momento])


def tracking_error(live: pd.Series, shadow: pd.Series) -> dict[str, float]:
    """Divergenza fra le due equity curve sulle sole giornate presenti in entrambe.

    `daily_bps` e' la deviazione standard della differenza dei rendimenti giornalieri,
    `cumulative_bps` la distanza fra i due rendimenti totali sul periodo comune.
    """
    comune = live.dropna().align(shadow.dropna(), join="inner")
    a, b = comune
    if len(a) < 2:
        return {"daily_bps": 0.0, "cumulative_bps": 0.0, "days": float(len(a))}
    differenze = a.pct_change().dropna() - b.pct_change().dropna()
    cumulato = (a.iloc[-1] / a.iloc[0]) - (b.iloc[-1] / b.iloc[0])
    return {
        "daily_bps": float(differenze.std(ddof=1) * BPS) if len(differenze) > 1 else 0.0,
        "cumulative_bps": float(cumulato * BPS),
        "days": float(len(a)),
    }


def per_symbol_table(confronti: Sequence[FillComparison]) -> pd.DataFrame:
    """Slippage medio e mediano per simbolo, dai due lati."""
    righe = [
        {
            "symbol": c.symbol,
            "slippage_real_bps": c.slippage_real_bps,
            "slippage_theoretical_bps": c.slippage_theoretical_bps,
            "commission_real": c.real.commission if c.real else None,
            "commission_theoretical": c.theoretical.commission if c.theoretical else None,
        }
        for c in confronti
    ]
    if not righe:
        return pd.DataFrame(columns=["symbol", "slippage_real_mean_bps", "slippage_real_median_bps"])
    frame = pd.DataFrame(righe)
    aggregato = frame.groupby("symbol").agg(
        n=("symbol", "size"),
        slippage_real_mean_bps=("slippage_real_bps", "mean"),
        slippage_real_median_bps=("slippage_real_bps", "median"),
        slippage_theoretical_mean_bps=("slippage_theoretical_bps", "mean"),
        slippage_theoretical_median_bps=("slippage_theoretical_bps", "median"),
    )
    return aggregato.reset_index()


def summarize(
    confronti: Sequence[FillComparison],
    live_equity: pd.Series | None = None,
    shadow_equity: pd.Series | None = None,
    generated_at: date | None = None,
) -> ReconcileSummary:
    """Aggrega il confronto in una sintesi confrontabile nel tempo."""
    sommario = ReconcileSummary(generated_at=generated_at or date.today())
    sommario.n_matched = sum(1 for c in confronti if c.status == APPAIATO)
    sommario.n_solo_live = sum(1 for c in confronti if c.status == SOLO_LIVE)
    sommario.n_solo_shadow = sum(1 for c in confronti if c.status == SOLO_SHADOW)

    reali = [c.slippage_real_bps for c in confronti if c.slippage_real_bps is not None]
    teorici = [c.slippage_theoretical_bps for c in confronti if c.slippage_theoretical_bps is not None]
    differenze = [c.diff_bps for c in confronti if c.diff_bps is not None]
    sommario.slippage_real_mean_bps = float(pd.Series(reali).mean()) if reali else 0.0
    sommario.slippage_real_median_bps = float(pd.Series(reali).median()) if reali else 0.0
    sommario.slippage_theoretical_mean_bps = float(pd.Series(teorici).mean()) if teorici else 0.0
    sommario.slippage_theoretical_median_bps = float(pd.Series(teorici).median()) if teorici else 0.0
    sommario.diff_mean_bps = float(pd.Series(differenze).mean()) if differenze else 0.0

    sommario.cost_real = sum(c.real.commission for c in confronti if c.real)
    sommario.cost_theoretical = sum(c.theoretical.commission for c in confronti if c.theoretical)
    sommario.per_symbol = per_symbol_table(confronti)

    if live_equity is not None and shadow_equity is not None:
        errore = tracking_error(live_equity, shadow_equity)
        sommario.tracking_error_daily_bps = errore["daily_bps"]
        sommario.tracking_error_cumulative_bps = errore["cumulative_bps"]
        sommario.days_compared = int(errore["days"])
    return sommario


def write_report(
    confronti: Sequence[FillComparison],
    sommario: ReconcileSummary,
    directory: str | Path = REPORT,
    giorno: date | None = None,
) -> tuple[Path, Path]:
    """Scrive il CSV del giorno e aggiunge una riga allo storico."""
    cartella = Path(directory)
    cartella.mkdir(parents=True, exist_ok=True)
    giorno = giorno or sommario.generated_at

    dettaglio = cartella / f"reconcile_{giorno.isoformat()}.csv"
    frame = pd.DataFrame([c.as_row() for c in confronti])
    frame.to_csv(dettaglio, index=False)

    storico = cartella / STORICO
    riga = pd.DataFrame([sommario.as_row()])
    riga.to_csv(storico, mode="a", header=not storico.exists(), index=False)

    logger.info(
        "report_scritto",
        dettaglio=str(dettaglio),
        storico=str(storico),
        appaiati=sommario.n_matched,
        solo_live=sommario.n_solo_live,
        solo_shadow=sommario.n_solo_shadow,
    )
    return dettaglio, storico


def live_equity_series(store: StateStore, days: int = 400) -> pd.Series:
    """Equity live registrata giorno per giorno."""
    righe = store.load_equity(days)
    if not righe:
        return pd.Series(dtype="float64")
    return pd.Series(
        [totale for _, _, _, totale in righe],
        index=pd.DatetimeIndex([pd.Timestamp(giorno) for giorno, _, _, _ in righe]),
        dtype="float64",
    )


def live_exposure_series(store: StateStore, days: int = 400) -> pd.Series:
    """Valore delle posizioni in frazione dell'equity, giorno per giorno.

    Lo StateStore salva il valore netto delle posizioni: per un portafoglio solo long,
    come quello del live, coincide con l'esposizione lorda.
    """
    righe = [(giorno, valore / totale) for giorno, _, valore, totale in store.load_equity(days) if totale > 0]
    if not righe:
        return pd.Series(dtype="float64")
    return pd.Series(
        [esposizione for _, esposizione in righe],
        index=pd.DatetimeIndex([pd.Timestamp(giorno) for giorno, _ in righe]),
        dtype="float64",
    )


def real_fill_events(store: StateStore, aperture: dict[str, pd.Series] | None = None) -> list[FillEvent]:
    """Eseguiti reali come FillEvent, con lo slippage in valuta misurato sull'apertura ufficiale.

    E' la grandezza che il backtest mette in `slippage_cost`, cosi' `compute_metrics`
    somma costi confrontabili fra live e shadow. Il segno e' positivo quando l'esecuzione
    e' sfavorevole; senza apertura di riferimento lo slippage vale zero.
    """
    aperture = aperture or {}
    eventi: list[FillEvent] = []
    for fill in store.load_fills():
        apertura = _apertura(aperture, fill.symbol, pd.Timestamp(fill.timestamp).date())
        costo = 0.0
        if apertura is not None and apertura > 0:
            costo = _segno(str(fill.direction)) * (fill.fill_price - apertura) * fill.quantity
        eventi.append(replace(fill, slippage_cost=costo))
    return eventi
