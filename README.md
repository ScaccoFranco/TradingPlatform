# quant - motore di backtesting event-driven
Barre daily, nessun look-ahead per costruzione: la strategia vede solo le barre fino al
cursore e un ordine generato sulla barra T e' eseguito all'open della barra T+1.

## Setup
    uv sync

## Dati
    uv run python scripts/download.py [SIMBOLI]  # default: universo completo dal 2005

## Backtest, analisi e test
    uv run python examples/buy_and_hold.py
    uv run python examples/momentum_insample.py  # 2005-2018, grafico e sensitivity
    uv run python examples/momentum_oos.py       # 2019-oggi, parametri di default
    uv run pytest

## Moduli
events (eventi frozen), data (cursore e barre visibili), strategy (intenzioni), portfolio
(quantita' ed equity curve), risk (limiti), execution (fill e costi), engine (loop),
analysis (metriche), calendar (inizio mese).
