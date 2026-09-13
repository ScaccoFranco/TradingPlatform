"""Corporate actions in casa: adj_close calcolato a mano, le due contabilita' del total return."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
from sintetici import adj_coerente, barre_sorgente

from quant.adjust import adjust, adjusted_view
from quant.config import BacktestConfig
from quant.data import ParquetDataHandler
from quant.download import in_formato_parquet
from quant.events import FillEvent, OrderDirection
from quant.portfolio import Portfolio
from quant.strategies.fixed_weights import FixedWeightsStrategy
from quant.strategy import BuyAndHoldStrategy, Strategy
from quant.validation import run_backtest


def serie_a_mano() -> pd.DataFrame:
    """Sei barre: cedola da 2 prima dello split 2:1, cedola da 0.5 dopo."""
    indice = pd.DatetimeIndex(
        ["2018-06-01", "2018-06-04", "2018-06-05", "2018-06-06", "2018-06-07", "2018-06-08"], name="date"
    )
    close = [100.0, 99.0, 100.0, 51.0, 50.0, 52.0]
    return pd.DataFrame(
        {
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1_000.0,
            "dividend": [0.0, 2.0, 0.0, 0.0, 0.5, 0.0],
            "split": [1.0, 1.0, 1.0, 2.0, 1.0, 1.0],
        },
        index=indice,
    )


def test_adj_close_calcolato_a_mano() -> None:
    """Passi alle date ex: 0.98 per la prima cedola, 1/2 per lo split, 50.5/51 per la seconda.

    L'ultima barra ha fattore 1; ogni barra prende il prodotto dei passi successivi.
    """
    atteso = [
        100.0 * 0.98 * 0.5 * 50.5 / 51.0,
        99.0 * 0.5 * 50.5 / 51.0,
        100.0 * 0.5 * 50.5 / 51.0,
        51.0 * 50.5 / 51.0,
        50.0,
        52.0,
    ]
    rettificato = adjust(serie_a_mano())
    assert list(rettificato["adj_close"]) == pytest.approx(atteso, rel=1e-12)
    assert list(rettificato["adj_volume"]) == [2_000.0, 2_000.0, 2_000.0, 1_000.0, 1_000.0, 1_000.0]
    assert list(rettificato["close"]) == list(serie_a_mano()["close"])


def test_rendimenti_rettificati_reinvestono_la_cedola_al_prezzo_ex() -> None:
    adj = adjust(serie_a_mano())["adj_close"]
    assert adj.iloc[1] / adj.iloc[0] == pytest.approx(99.0 / (100.0 - 2.0))
    assert adj.iloc[3] / adj.iloc[2] == pytest.approx(51.0 * 2.0 / 100.0)
    assert adj.iloc[4] / adj.iloc[3] == pytest.approx(50.0 / (51.0 - 0.5))


def test_adjust_sostituisce_l_adj_close_del_vendor() -> None:
    barre = barre_sorgente(pd.bdate_range("2018-01-02", periods=40), cedole={"2018-01-19": 0.6})
    barre["adj_close"] = 1.0
    assert list(adjust(barre)["adj_close"]) == pytest.approx(list(adj_coerente(barre)), rel=1e-12)


def test_adjust_accetta_anche_i_nomi_dei_parquet() -> None:
    parquet = serie_a_mano().rename(columns={"dividend": "dividends", "split": "split_factor"})
    assert list(adjust(parquet)["adj_close"]) == list(adjust(serie_a_mano())["adj_close"])


def test_vista_rettificata_senza_operazioni_sul_capitale() -> None:
    parquet = in_formato_parquet(serie_a_mano())
    vista = adjusted_view(parquet)
    assert list(vista["close"]) == pytest.approx(list(parquet["adj_close"]), rel=1e-12)
    assert (vista["dividends"] == 0.0).all() and (vista["split_factor"] == 1.0).all()
    assert list(vista["adj_close"]) == list(parquet["adj_close"])
    assert list(parquet["close"]) == list(serie_a_mano()["close"])


SEDUTE = pd.bdate_range("2018-02-01", periods=80)
CEDOLE = {"2018-02-22": 0.6, "2018-04-12": 0.4}
SPLIT = {"2018-03-15": 2.0}


def cartella_con_operazioni(tmp_path: Path) -> Path:
    """SPY con uno split 2:1 e due cedole, scritto come lo scrive il download."""
    cartella = tmp_path / "parquet"
    cartella.mkdir()
    barre = barre_sorgente(SEDUTE, cedole=CEDOLE, split=SPLIT, passo=0.03)
    in_formato_parquet(barre).to_parquet(cartella / "SPY.parquet")
    cash = barre_sorgente(SEDUTE, base=80.0, passo=0.004)
    in_formato_parquet(cash).to_parquet(cartella / "SHY.parquet")
    return cartella


def equity_finale(
    cartella: Path, dividends_as_cash: bool, factory: Callable[[], Strategy]
) -> tuple[float, float]:
    config = BacktestConfig(path=cartella, risk_free_symbol=None, dividends_as_cash=dividends_as_cash)
    risultato = run_backtest(factory, ["SPY", "SHY"], config=config)
    return risultato["equity_curve"][-1][1], risultato["portfolio"].dividends_received


def test_buy_and_hold_con_cedole_in_cassa_uguale_a_adj_close(tmp_path: Path) -> None:
    cartella = cartella_con_operazioni(tmp_path)
    in_cassa, cedole = equity_finale(cartella, True, lambda: BuyAndHoldStrategy(None, "SPY"))
    rettificata, nessuna = equity_finale(cartella, False, lambda: BuyAndHoldStrategy(None, "SPY"))

    assert cedole > 0.0 and nessuna == 0.0
    assert in_cassa == pytest.approx(rettificata, rel=0.001)
    # la tolleranza discrimina: perdere le cedole sposterebbe l'equity ben oltre lo 0.1%
    assert abs((in_cassa - cedole) / rettificata - 1.0) > 0.005


def test_benchmark_mensile_uguale_nelle_due_contabilita(tmp_path: Path) -> None:
    cartella = cartella_con_operazioni(tmp_path)
    pesi = {"SPY": 0.6, "SHY": 0.4}
    in_cassa, _ = equity_finale(cartella, True, lambda: FixedWeightsStrategy(None, pesi))
    rettificata, _ = equity_finale(cartella, False, lambda: FixedWeightsStrategy(None, pesi))
    assert in_cassa == pytest.approx(rettificata, rel=0.001)


def test_contabilita_rettificata_richiede_il_data_handler_predefinito(tmp_path: Path) -> None:
    cartella = cartella_con_operazioni(tmp_path)
    with pytest.raises(ValueError, match="dividends_as_cash=False"):
        run_backtest(
            lambda: BuyAndHoldStrategy(None, "SPY"),
            ["SPY"],
            config=BacktestConfig(path=cartella, risk_free_symbol=None, dividends_as_cash=False),
            data_handler_factory=lambda path, symbols, start, end: ParquetDataHandler(path, list(symbols)),
        )


def portafoglio_alla_vigilia(cartella: Path) -> tuple[Portfolio, ParquetDataHandler, datetime]:
    """100 azioni SPY alla chiusura prima della data ex del 22 febbraio."""
    handler = ParquetDataHandler(cartella, ["SPY"])
    portfolio = Portfolio(handler, initial_cash=0.0, credit_dividends=True)
    data_ex = pd.Timestamp("2018-02-22")
    for _ in range(SEDUTE.get_loc(data_ex)):
        handler.update_bars()
    portfolio.positions["SPY"] = 100
    return portfolio, handler, data_ex.to_pydatetime()


def fill(quando: datetime, direzione: OrderDirection, quantita: int) -> FillEvent:
    return FillEvent(quando, "SPY", direzione, quantita, fill_price=0.0, commission=0.0)


def test_acquisto_alla_data_ex_non_prende_la_cedola(tmp_path: Path) -> None:
    portfolio, handler, data_ex = portafoglio_alla_vigilia(cartella_con_operazioni(tmp_path))
    evento = handler.update_bars()[0]
    portfolio.on_fill(fill(data_ex, OrderDirection.BUY, 50))
    portfolio.on_market(evento)
    assert portfolio.positions["SPY"] == 150
    assert portfolio.dividends_received == pytest.approx(100 * 0.6)


def test_vendita_alla_data_ex_incassa_ancora_la_cedola(tmp_path: Path) -> None:
    portfolio, handler, data_ex = portafoglio_alla_vigilia(cartella_con_operazioni(tmp_path))
    evento = handler.update_bars()[0]
    portfolio.on_fill(fill(data_ex, OrderDirection.SELL, 100))
    portfolio.on_market(evento)
    assert portfolio.positions["SPY"] == 0
    assert portfolio.dividends_received == pytest.approx(100 * 0.6)
