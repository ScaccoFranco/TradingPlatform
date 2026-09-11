"""Cosa gira in live: universo, strategia e data di avvio, in un solo posto.

Scheduler e report settimanale devono parlare della stessa strategia sugli stessi
simboli: tenerne due copie separate era il modo piu' semplice per farle divergere.
"""

from __future__ import annotations

from quant.strategies.momentum import CrossSectionalMomentum
from quant.strategy import Strategy

UNIVERSO = ["SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "TLT", "IEF", "LQD", "HYG", "GLD", "DBC"]
CASH = "SHY"
SIMBOLI = [*UNIVERSO, CASH]
STRATEGIA = "momentum_v1"
AVVIO_LIVE = "2026-09-01"


def strategy_factory() -> Strategy:
    """La strategia del live, senza data handler: lo collega chi la esegue."""
    return CrossSectionalMomentum(None, UNIVERSO, cash_symbol=CASH)
