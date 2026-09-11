"""Test dei costi espliciti, del vincolo di cassa e delle cedole."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from quant.analysis import compute_metrics
from quant.data import ParquetDataHandler
from quant.engine import Backtest
from quant.events import OrderDirection, OrderEvent
from quant.execution import SimulatedExecutionHandler
from quant.portfolio import Portfolio
from quant.strategy import BuyAndHoldStrategy

CAPITALE = 100_000.0


def esegui(
    directory: Path,
    commissione: float = 0.0,
    slippage: float = 0.0,
    cash_buffer: float = 0.0,
    credit_dividends: bool = False,
) -> Portfolio:
    """Buy-and-hold su SPY con la configurazione di portafoglio indicata."""
    handler = ParquetDataHandler(directory, ["SPY"])
    portfolio = Portfolio(
        handler,
        initial_cash=CAPITALE,
        cash_buffer=cash_buffer,
        credit_dividends=credit_dividends,
    )
    execution = SimulatedExecutionHandler(handler, commission_per_trade=commissione, slippage_bps=slippage)
    Backtest(handler, BuyAndHoldStrategy(handler, "SPY"), portfolio, None, execution).run()
    return portfolio


def test_slippage_registrato_nei_fill_e_nelle_metriche(spy_parquet_dir: Path) -> None:
    portfolio = esegui(spy_parquet_dir, commissione=5.0, slippage=20.0)
    fill = portfolio.fills[0]
    barre = pd.read_parquet(spy_parquet_dir / "SPY.parquet")
    atteso = barre["open"].iloc[1] * 0.002 * fill.quantity
    assert fill.slippage_cost == pytest.approx(atteso)

    metriche = compute_metrics(portfolio.equity_curve, portfolio.fills)
    assert metriche["commissioni"] == 5.0
    assert metriche["slippage"] == pytest.approx(atteso)
    assert metriche["costi_totali"] == pytest.approx(atteso + 5.0)


def test_senza_slippage_il_costo_e_nullo(spy_parquet_dir: Path) -> None:
    portfolio = esegui(spy_parquet_dir)
    assert portfolio.fills[0].slippage_cost == 0.0
    assert compute_metrics(portfolio.equity_curve, portfolio.fills)["costi_totali"] == 0.0


def test_cassa_negativa_solo_senza_cuscinetto(gap_up_parquet_dir: Path) -> None:
    """Il gap fra close di dimensionamento e open di eseguito e' l'unico modo di sforare."""
    senza = esegui(gap_up_parquet_dir)
    assert senza.cash < 0.0

    con = esegui(gap_up_parquet_dir, cash_buffer=0.05)
    assert con.cash > 0.0
    assert con.positions["SPY"] < senza.positions["SPY"]


def test_la_cassa_limita_la_quantita_ordinata(spy_parquet_dir: Path) -> None:
    portfolio = esegui(spy_parquet_dir, cash_buffer=0.5)
    barre = pd.read_parquet(spy_parquet_dir / "SPY.parquet")
    assert portfolio.positions["SPY"] == int(CAPITALE * 0.5 // barre["close"].iloc[0])


def test_cedola_accreditata_in_cassa(corporate_actions_parquet_dir: Path) -> None:
    """La cassa cresce esattamente di quantita' per cedola, letta dalla colonna dividends."""
    con_cedole = esegui(corporate_actions_parquet_dir, credit_dividends=True)
    senza_cedole = esegui(corporate_actions_parquet_dir, credit_dividends=False)
    quantita = con_cedole.positions["SPY"]

    assert con_cedole.dividends_received == pytest.approx(quantita * 1.0)
    assert senza_cedole.dividends_received == 0.0
    assert con_cedole.cash - senza_cedole.cash == pytest.approx(quantita * 1.0)


def test_nessuna_cedola_fantasma_su_prezzi_lisci(spy_parquet_dir: Path) -> None:
    """Senza colonna dividends non deve comparire nessun accredito."""
    portfolio = esegui(spy_parquet_dir, credit_dividends=True)
    assert portfolio.dividends_received == 0.0


def test_lo_split_lascia_invariato_il_valore(corporate_actions_parquet_dir: Path) -> None:
    """Attraverso uno split 2:1 il portafoglio cambia solo per la frazione liquidata."""
    handler = ParquetDataHandler(corporate_actions_parquet_dir, ["SPY"])
    portfolio = Portfolio(handler, initial_cash=CAPITALE)
    for _ in range(10):
        handler.update_bars()
    portfolio.positions["SPY"] = 101
    portfolio.cash = 0.0
    prima = portfolio.total_value()

    evento = handler.update_bars()[0]
    assert handler.get_latest_bars("SPY", 1)["split_factor"].iloc[-1] == 2.0
    portfolio.on_market(evento)

    assert portfolio.positions["SPY"] == 202
    assert portfolio.total_value() == pytest.approx(prima)
    assert portfolio.cash == 0.0


def test_lo_split_liquida_il_residuo_frazionario(corporate_actions_parquet_dir: Path) -> None:
    """Un raggruppamento inverso lascia mezza azione: viene monetizzata all'apertura."""
    handler = ParquetDataHandler(corporate_actions_parquet_dir, ["SPY"])
    frame = pd.read_parquet(corporate_actions_parquet_dir / "SPY.parquet")
    frame.loc[frame.index[10], "split_factor"] = 0.5
    frame.to_parquet(corporate_actions_parquet_dir / "SPY.parquet")

    handler = ParquetDataHandler(corporate_actions_parquet_dir, ["SPY"])
    portfolio = Portfolio(handler, initial_cash=0.0)
    for _ in range(10):
        handler.update_bars()
    portfolio.positions["SPY"] = 101

    evento = handler.update_bars()[0]
    apertura = float(handler.get_latest_bars("SPY", 1)["open"].iloc[-1])
    portfolio.on_market(evento)

    assert portfolio.positions["SPY"] == 50
    assert portfolio.cash == pytest.approx(0.5 * apertura)


def test_ordine_pendente_attraverso_lo_split(corporate_actions_parquet_dir: Path) -> None:
    """Un ordine deciso prima dello split viene riempito nella scala nuova."""
    handler = ParquetDataHandler(corporate_actions_parquet_dir, ["SPY"])
    esecuzione = SimulatedExecutionHandler(handler)
    for _ in range(10):
        handler.update_bars()

    ordine = OrderEvent(handler.current_timestamp(), "SPY", OrderDirection.BUY, 100)
    esecuzione.on_order(ordine)

    evento = handler.update_bars()[0]
    fills = esecuzione.on_market(evento)
    assert len(fills) == 1
    assert fills[0].quantity == 200
    assert fills[0].fill_price == 50.0
    assert fills[0].quantity * fills[0].fill_price == pytest.approx(ordine.quantity * 100.0)
