"""Configurazione live: solo variabili d'ambiente, mai segreti nel codice."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

PERCORSO_DATI = Path("data/parquet")


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Tutti i parametri di un backtest in un solo oggetto immutabile.

    Prima vivevano sparsi in un dizionario negli script di ricerca, dove nessuno li
    controllava: un refuso in una chiave passava inosservato e cambiava i risultati.
    """

    initial_cash: float = 100_000.0
    commission_per_trade: float = 1.0
    slippage_bps: float = 5.0
    cash_buffer: float = 0.01
    credit_dividends: bool = True
    max_weight_per_symbol: float = 1.0
    max_gross_exposure: float = 1.0
    max_drawdown: float = 1.0
    risk_free_symbol: str | None = "SHY"
    path: Path = PERCORSO_DATI

    def with_overrides(self, **campi: Any) -> BacktestConfig:
        """Copia con alcuni campi sostituiti, per le griglie di parametri."""
        sconosciuti = set(campi) - {f for f in self.__dataclass_fields__}
        if sconosciuti:
            raise TypeError(f"parametri non riconosciuti: {sorted(sconosciuti)}")
        return replace(self, **campi)


class ConfigError(RuntimeError):
    """Configurazione mancante o non ammessa per il trading live."""


class Settings(BaseSettings):
    """Impostazioni lette da ambiente o da un file `.env` non committato."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_paper: bool = True
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    def require_paper(self) -> None:
        """Blocca l'avvio se non si sta operando in paper trading.

        Il trading con denaro reale non e' nello scope: meglio un errore all'avvio
        che un ordine vero partito per una variabile d'ambiente dimenticata.
        """
        if not self.alpaca_paper:
            raise ConfigError("ALPACA_PAPER deve essere true: il trading reale non e' supportato")

    def require_credentials(self) -> None:
        """Verifica che le chiavi API siano presenti."""
        mancanti = [
            nome
            for nome, valore in (
                ("ALPACA_API_KEY", self.alpaca_api_key),
                ("ALPACA_SECRET_KEY", self.alpaca_secret_key),
            )
            if not valore
        ]
        if mancanti:
            raise ConfigError(f"variabili d'ambiente mancanti: {', '.join(mancanti)}")

    def validate_live(self) -> Settings:
        """Controlli obbligatori prima di parlare con il broker."""
        self.require_paper()
        self.require_credentials()
        return self

    @property
    def telegram_enabled(self) -> bool:
        """True se gli alert Telegram sono configurati."""
        return bool(self.telegram_bot_token and self.telegram_chat_id)


def load_settings(**overrides: Any) -> Settings:
    """Carica le impostazioni dall'ambiente, con override espliciti per i test."""
    return Settings(**overrides)
