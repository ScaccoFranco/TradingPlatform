# quant - motore di backtesting event-driven
Barre daily, nessun look-ahead per costruzione: la strategia vede solo le barre fino al
cursore e un ordine generato sulla barra T e' eseguito all'open della barra T+1. Le stesse
classi girano in backtest e in paper trading. Architettura e scelte in `CLAUDE.md`.

## Setup e dati
    uv sync
    uv run python scripts/download.py [--update] [SIMBOLI]  # default: universo dal 2005

## Ricerca e qualita'
    uv run python -m quant.research.insample     # 2005-2018, grafico e sensitivity
    uv run python -m quant.research.oos          # 2019-oggi, parametri di default
    uv run pytest && uv run ruff check . && uv run mypy quant

## Paper trading
    uv run python scripts/live.py                # scheduler, guida in docs/live.md
    uv run python scripts/status.py              # posizioni, equity, ultimi ordini
    uv run python scripts/weekly_report.py       # live contro shadow, docs/operations.md

## Moduli: events, data, download, strategy, portfolio, risk, execution, engine, analysis, validation, config, logging, research/, live/, brokers/
