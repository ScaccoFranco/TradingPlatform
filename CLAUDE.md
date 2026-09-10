# quant - motore di backtesting event-driven

Backtest su barre daily di azioni ed ETF. L'assenza di look-ahead e' una proprieta'
strutturale, non un controllo a posteriori: lo stesso codice deve poter girare in live.

## Comandi

    uv sync                                      # ambiente, Python 3.12
    uv run python scripts/download.py [SIMBOLI]  # dati in data/parquet/{symbol}.parquet
    uv run python examples/buy_and_hold.py       # backtest di esempio
    uv run pytest                                # suite completa, offline

## Architettura

Un solo loop in `quant/engine.py`: `update_bars()` mette in coda un MarketEvent per data,
la coda viene svuotata dispatchando per tipo, si ripete finche' ci sono barre. Su ogni
barra l'ordine delle fasi e' fisso: fill degli ordini pendenti all'open, mark-to-market
sul close, poi la strategia.

    MarketEvent -> Strategy -> SignalEvent -> Portfolio -> OrderEvent
                -> RiskManager -> ExecutionHandler -> FillEvent -> Portfolio

| File | Responsabilita' |
|---|---|
| `quant/events.py` | Quattro dataclass frozen, nessuna logica |
| `quant/data.py` | Cursore sull'indice temporale unificato, barre visibili |
| `quant/strategy.py` | Strategy ABC e strategie di base |
| `quant/portfolio.py` | Unico componente che conosce il capitale |
| `quant/risk.py` | Peso per simbolo, esposizione lorda, drawdown |
| `quant/execution.py` | Fill a T+1 con commissioni e slippage |
| `quant/analysis.py` | Metriche e tabelle di confronto |
| `quant/calendar.py` | Rilevamento dell'inizio mese |

## Regole di dominio, non negoziabili

1. La strategia legge i dati solo con `get_latest_bars` o `get_latest_bars_all`, che
   troncano al cursore. Il dataset completo non e' mai esposto.
2. Un ordine generato sulla barra T viene eseguito all'open di T+1, mai al close di T.
3. Segnali e rendimenti usano `adj_close`; quantita' e prezzi di eseguito usano i prezzi
   non aggiustati.
4. La strategia emette intenzioni, cioe' direzione e forza in [0, 1], mai quantita'.
5. Ogni OrderEvent passa dal RiskManager prima dell'ExecutionHandler.
6. Commissioni e slippage sono parametri del costruttore, mai costanti nel codice.

## Scelte di progetto da conoscere

- **Ribilanciamento sulla prima barra del mese.** Sapere se una barra e' l'ultima del
  mese richiede il calendario futuro; sapere se e' la prima no. `quant/calendar.py`
  espone entrambe le funzioni ma solo `is_first_trading_day_of_month` e' un trigger
  valido in live.
- **Cedole.** `Portfolio(credit_dividends=True)` ricava la cedola dal rapporto fra
  adj_close e close e la accredita in cassa, dove resta fino al ribilanciamento
  successivo. Il default e' False, cioe' solo rendimento di prezzo.
- **Cassa.** Il dimensionamento usa il close di oggi ma il fill avviene all'open di
  domani: `cash_buffer` protegge dai gap al rialzo, con zero la cassa puo' andare
  di poco negativa.
- **Costi.** `FillEvent.slippage_cost` tiene lo slippage separato dalla commissione,
  cosi' le metriche mostrano le due voci distinte.
- **RiskManager.** Le riduzioni di posizione passano sempre, anche oltre soglia di
  drawdown: i limiti valgono per chi aumenta il rischio, non per chi lo chiude.
- **Ordini della stessa barra.** Vendite e acquisti di un ribilanciamento avvengono
  allo stesso open, quindi il Portfolio proietta cassa e posizioni con gli ordini gia'
  emessi e il RiskManager valuta l'esposizione su quella proiezione. Senza questa
  compensazione una rotazione completa sembra un raddoppio di esposizione e gli
  acquisti vengono rifiutati.
- **Riscaldamento.** `run_backtest(warmup_start=...)` fa partire il backtest prima e
  misura solo da `start`, riscalando l'equity al capitale iniziale. Serve alle strategie
  con lookback lungo, che altrimenti passerebbero in cassa il primo anno di ogni finestra.

## Convenzioni

Python 3.12, dipendenze runtime pandas, pyarrow, duckdb, yfinance, piu' matplotlib dalla
Fase 2. Nessun framework di backtesting esterno. Type hints ovunque, dataclass frozen per
gli eventi, ABC per le interfacce. Docstring brevi in italiano, niente commenti superflui.
I test non toccano la rete: usano Parquet sintetici costruiti nelle fixture. I dati dal
2019-01-01 in poi sono riservati alla validazione out-of-sample e non vanno usati nei test
ne' negli script di sviluppo.

## Stato

Fasi 1 e 2 complete. `quant/strategies/` contiene il momentum cross-sectional e il
benchmark a pesi fissi, `quant/validation.py` walk-forward, sensitivita' e Deflated
Sharpe. Gli script di analisi stanno in `examples/`, gli output in `reports/`.
Il confronto usa benchmark ribilanciati mensilmente: senza ribilanciamento le cedole
resterebbero ferme in cassa e il paragone favorirebbe la strategia attiva.
