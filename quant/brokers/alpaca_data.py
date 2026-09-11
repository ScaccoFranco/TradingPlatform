"""Barre daily da Alpaca, con la stessa interfaccia del data handler su Parquet."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from quant.config import Settings, load_settings
from quant.data import COLUMNS, FrameDataHandler
from quant.logging import get_logger

logger = get_logger("alpaca.data")

GIORNI_DI_STORIA = 500


class AlpacaDataHandler(FrameDataHandler):
    """Storico daily scaricato dall'API, poi identico a `ParquetDataHandler`.

    Le barre grezze danno i prezzi per quantita' ed eseguiti, quelle rettificate
    solo la colonna `adj_close` usata dai segnali: la regola di dominio sui prezzi
    aggiustati vale anche in live.
    """

    def __init__(
        self,
        client: Any,
        symbols: list[str],
        start: date | datetime | str | None = None,
        end: date | datetime | str | None = None,
        lookback_days: int = GIORNI_DI_STORIA,
    ) -> None:
        self.client = client
        fine = pd.Timestamp(end) if end is not None else pd.Timestamp.utcnow().normalize().tz_localize(None)
        inizio = pd.Timestamp(start) if start is not None else fine - timedelta(days=lookback_days)
        super().__init__(self._scarica(symbols, inizio, fine))

    @classmethod
    def from_settings(
        cls, symbols: list[str], settings: Settings | None = None, **kwargs: Any
    ) -> AlpacaDataHandler:
        """Costruisce il client storico dopo aver validato la configurazione."""
        from alpaca.data.historical import StockHistoricalDataClient

        impostazioni = (settings or load_settings()).validate_live()
        client = StockHistoricalDataClient(
            api_key=impostazioni.alpaca_api_key,
            secret_key=impostazioni.alpaca_secret_key,
        )
        return cls(client, symbols, **kwargs)

    def _scarica(
        self, symbols: list[str], inizio: pd.Timestamp, fine: pd.Timestamp
    ) -> dict[str, pd.DataFrame]:
        """Unisce barre grezze e rettificate in un frame per simbolo."""
        grezze = self._richiedi(symbols, inizio, fine, "raw")
        rettificate = self._richiedi(symbols, inizio, fine, "all")
        dati: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            frame = grezze.get(symbol)
            if frame is None or frame.empty:
                logger.warning("nessuna_barra", symbol=symbol)
                dati[symbol] = pd.DataFrame(columns=list(COLUMNS), index=pd.DatetimeIndex([], name="date"))
                continue
            aggiustato = rettificate.get(symbol)
            if aggiustato is not None and not aggiustato.empty:
                frame["adj_close"] = aggiustato["close"].reindex(frame.index)
            frame["adj_close"] = frame["adj_close"].fillna(frame["close"])
            dati[symbol] = frame[list(COLUMNS)]
        return dati

    def _richiedi(
        self,
        symbols: list[str],
        inizio: pd.Timestamp,
        fine: pd.Timestamp,
        adjustment: str,
    ) -> dict[str, pd.DataFrame]:
        """Una chiamata all'API storica, normalizzata in frame per simbolo."""
        from alpaca.data.enums import Adjustment
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        try:
            risposta = self.client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=list(symbols),
                    timeframe=TimeFrame.Day,
                    start=inizio.to_pydatetime(),
                    end=fine.to_pydatetime(),
                    adjustment=Adjustment(adjustment),
                )
            )
        except Exception as errore:  # noqa: BLE001 - senza barre rettificate si prosegue sui prezzi grezzi
            logger.warning("richiesta_barre_fallita", adjustment=adjustment, errore=str(errore))
            return {}
        return self._normalizza(risposta)

    @staticmethod
    def _normalizza(risposta: Any) -> dict[str, pd.DataFrame]:
        """Trasforma la risposta dell'SDK in un frame per simbolo, indicizzato per data."""
        frame = getattr(risposta, "df", None)
        if frame is None or frame.empty:
            return {}
        frame = frame.reset_index()
        dati: dict[str, pd.DataFrame] = {}
        for symbol, gruppo in frame.groupby("symbol"):
            momenti = pd.to_datetime(gruppo["timestamp"], utc=True).dt.tz_localize(None)
            indice = pd.DatetimeIndex(momenti.dt.normalize())
            per_simbolo = pd.DataFrame(
                {
                    "open": gruppo["open"].to_numpy(dtype="float64"),
                    "high": gruppo["high"].to_numpy(dtype="float64"),
                    "low": gruppo["low"].to_numpy(dtype="float64"),
                    "close": gruppo["close"].to_numpy(dtype="float64"),
                    "adj_close": gruppo["close"].to_numpy(dtype="float64"),
                    "volume": gruppo["volume"].to_numpy(dtype="float64"),
                    # l'API delle barre non porta le operazioni sul capitale: le colonne
                    # restano neutre e le cedole live arrivano dalla cassa del broker
                    "dividends": 0.0,
                    "split_factor": 1.0,
                },
                index=indice,
            )
            per_simbolo.index.name = "date"
            dati[str(symbol)] = per_simbolo.sort_index()
        return dati
