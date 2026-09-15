# quant - motore di backtesting event-driven

Backtest su barre daily di azioni ed ETF. L'assenza di look-ahead e' una proprieta'
strutturale, non un controllo a posteriori: lo stesso codice deve poter girare in live.

## Comandi

    uv sync                                            # ambiente, Python 3.12
    uv run python scripts/download.py [SIMBOLI]        # storico completo da Tiingo in data/parquet/
    uv run python scripts/download.py --update         # solo le barre nuove
    uv run python scripts/download.py --check          # riscarica e confronta, senza scrivere
    uv run python scripts/download.py --source yfinance SPY   # secondo fornitore, per confronto
    uv run python -m quant.research.buy_and_hold       # esempio minimo
    uv run python -m quant.research.insample           # 2005-2018, grafico e sensitivity
    uv run python -m quant.research.dividend_accounting  # le due contabilita' delle cedole
    uv run python -m quant.research.oos                # 2019-oggi, parametri di default
    uv run python scripts/live.py                      # scheduler del paper trading
    uv run python scripts/status.py                    # posizioni, equity, ultimi ordini
    uv run python scripts/weekly_report.py             # live contro shadow backtest
    uv run python scripts/ui.py                        # dashboard di sola lettura su 127.0.0.1:8000

## Architettura

Un solo loop in `quant/engine.py`: `update_bars()` mette in coda un MarketEvent per data,
la coda viene svuotata dispatchando per tipo, si ripete finche' ci sono barre. Su ogni
barra l'ordine delle fasi e' fisso: fill degli ordini pendenti all'open, mark-to-market
sul close, poi la strategia.

    MarketEvent -> Strategy -> SignalEvent -> Portfolio -> OrderEvent
                -> RiskManager -> ExecutionHandler -> FillEvent -> Portfolio

## Struttura del pacchetto

| Percorso | Responsabilita' |
|---|---|
| `quant/events.py` | Quattro dataclass frozen, nessuna logica |
| `quant/data.py` | Cursore sull'indice temporale unificato, barre visibili |
| `quant/sources.py` | `DataSource` ABC, Tiingo e yfinance, contratto delle barre in ingresso |
| `quant/download.py` | Orchestrazione: scarica, controlla, scrive in modo atomico, aggiorna il manifest |
| `quant/data_quality.py` | Controlli in ingresso, severita' dei finding, calendario di borsa |
| `quant/adjust.py` | `adj_close` e `adj_volume` dai grezzi: l'unico posto che li produce |
| `quant/manifest.py` | `data/manifest.json`: sorgente, intervallo, barre e SHA-256 per simbolo |
| `quant/provenance.py` | Impronta di un backtest: dati, commit, config, finestra, strategia |
| `quant/drift.py` | `--check`: differenze retroattive fra fornitore e Parquet salvati |
| `quant/strategy.py` | Strategy ABC e strategie di base |
| `quant/strategies/` | Momentum cross-sectional e benchmark a pesi fissi |
| `quant/portfolio.py` | Unico componente che conosce il capitale |
| `quant/risk.py` | Limiti, kill switch, decisioni tracciate in `RiskDecision` |
| `quant/execution.py` | Fill a T+1 con commissioni e slippage |
| `quant/engine.py` | Il loop degli eventi |
| `quant/analysis.py` | Metriche e tabelle di confronto |
| `quant/calendar.py` | Rilevamento dell'inizio mese |
| `quant/config.py` | `BacktestConfig` per il backtest, `Settings` per il live |
| `quant/logging.py` | structlog: JSON su file in live, console in sviluppo |
| `quant/validation.py` | `run_backtest`, walk-forward, sensitivita', Deflated Sharpe |
| `quant/research/` | Script di ricerca, eseguibili con `python -m` |
| `quant/state.py`, `quant/brokers/`, `quant/live/` | Paper trading, vedi Fase 3 |
| `quant/shadow.py`, `quant/reconcile_report.py`, `quant/logreader.py`, `quant/weekly.py` | Confronto live, vedi Fase 4 |
| `quant/webui/` | Dashboard locale di sola lettura: strato dati, avvisi, grafici SVG, template, vedi Fase 6 |
| `scripts/` | Involucri sottili da riga di comando, nessuna logica propria |

## Regole di dominio, non negoziabili

1. La strategia legge i dati solo con `get_latest_bars` o `get_latest_bars_all`, che
   troncano al cursore. Il dataset completo non e' mai esposto.
2. Un ordine generato sulla barra T viene eseguito all'open di T+1, mai al close di T.
3. Segnali e rendimenti usano `adj_close`; quantita' e prezzi di eseguito usano i prezzi
   non aggiustati.
4. La strategia emette intenzioni, cioe' direzione e forza in [0, 1], mai quantita'.
5. Ogni OrderEvent passa dal RiskManager prima dell'ExecutionHandler.
6. Commissioni e slippage sono parametri del costruttore, mai costanti nel codice.
7. L'`adj_close` si calcola in casa, in `quant/adjust.py`, mai si prende dal fornitore:
   quello del vendor si scarica solo per confrontarlo nei controlli di qualita'.
8. Nessuna barra entra nei Parquet senza passare dai controlli di `quant/data_quality.py`,
   e un `BLOCKING` lascia sul disco la versione precedente.

## Scelte di progetto da conoscere

- **Ribilanciamento sulla prima barra del mese.** Sapere se una barra e' l'ultima del
  mese richiede il calendario futuro; sapere se e' la prima no. `quant/calendar.py`
  espone entrambe le funzioni ma solo `is_first_trading_day_of_month` e' un trigger
  valido in live.
- **Prezzi grezzi, operazioni sul capitale esplicite.** I Parquet contengono prezzi non
  rettificati per gli split, piu' le colonne `dividends` e `split_factor`. E' la stessa
  convenzione del feed del broker: allo split il prezzo scende e le azioni aumentano.
  Tiingo consegna i grezzi cosi' come sono; `YFinanceSource` smonta la scala gia'
  rettificata moltiplicando le barre per gli split successivi. `--update` accoda solo le
  barre nuove e non riscrive mai quelle salvate, nemmeno dopo una cedola, perche'
  `adj_close` si ricalcola in casa su tutta la storia. La seduta di oggi resta fuori
  finche' non e' chiusa: a mercato aperto il close e' provvisorio e nessuno lo
  correggerebbe piu'.
- **Cedole e split.** `Portfolio.on_market` applica prima lo split della barra, con la
  frazione residua monetizzata all'apertura, poi accredita `dividends` per azione in
  cassa, e solo alla fine valorizza. Lo split vale sempre, le cedole solo con
  `dividends_as_cash=True`, il default e l'unica contabilita' possibile in live: ne ha
  diritto chi aveva le azioni alla chiusura precedente la data ex, non chi compra quel
  giorno. Restano liquide fino al ribilanciamento successivo. Con `dividends_as_cash=False`
  le barre passano da `adjusted_view` e il total return sta tutto nel prezzo, utile come
  controprova: sull'in-sample le due contabilita' distano meno dello 0,2%.
  L'esecuzione riscala gli ordini pendenti che attraversano uno split.
- **Cassa.** Il dimensionamento usa il close di oggi ma il fill avviene all'open di
  domani: `cash_buffer` protegge dai gap al rialzo, con zero la cassa puo' andare
  di poco negativa.
- **Costi.** `FillEvent.slippage_cost` tiene lo slippage separato dalla commissione,
  cosi' le metriche mostrano le due voci distinte.
- **RiskManager.** Le riduzioni di posizione passano sempre, anche oltre soglia di
  drawdown: i limiti valgono per chi aumenta il rischio, non per chi lo chiude. Ogni
  chiamata a `filter` lascia una `RiskDecision` in `decisions`, con un motivo preso da
  `RiskReason`. Il drawdown si legge da `Portfolio.peak_equity`, aggiornato a ogni barra.
- **Metriche in eccesso.** `run_backtest` ricava il tasso privo di rischio dai rendimenti
  di `BacktestConfig.risk_free_symbol`, per default SHY. Sharpe e Sortino sono calcolati
  sui rendimenti in eccesso, CAGR, volatilita' e drawdown restano lordi.
- **Posizioni ferme.** Se un simbolo in portafoglio smette di avere barre, il Portfolio lo
  segnala una volta a WARNING e continua a valorizzarlo all'ultimo prezzo; il momentum
  lo toglie dalla classifica.
- **Ordini della stessa barra.** Vendite e acquisti di un ribilanciamento avvengono
  allo stesso open, quindi il Portfolio proietta cassa e posizioni con gli ordini gia'
  emessi e il RiskManager valuta l'esposizione su quella proiezione. Senza questa
  compensazione una rotazione completa sembra un raddoppio di esposizione e gli
  acquisti vengono rifiutati.
- **Riscaldamento.** `run_backtest(warmup_start=...)` fa partire il backtest prima e
  misura solo da `start`, riscalando l'equity al capitale iniziale. Serve alle strategie
  con lookback lungo, che altrimenti passerebbero in cassa il primo anno di ogni finestra.

## Convenzioni

Python 3.12. Dipendenze runtime: pandas, pyarrow, yfinance, requests, matplotlib, structlog,
piu' alpaca-py, apscheduler e pydantic-settings per il live, fastapi, uvicorn e jinja2 per la
dashboard; in sviluppo httpx per il `TestClient`. Nessun framework di backtesting
esterno. Type hints ovunque, dataclass frozen per gli eventi, ABC per le interfacce.
Docstring brevi in italiano, niente commenti superflui. I parametri di un backtest passano
da `BacktestConfig`: gli argomenti sciolti di `run_backtest` sono deprecati. Il pacchetto
e' installato in modalita' editabile, quindi nessun modulo manipola `sys.path`.
I test non toccano la rete: usano Parquet sintetici costruiti nelle fixture. I dati dal
2019-01-01 in poi sono riservati alla validazione out-of-sample e non vanno usati nei test
ne' negli script di sviluppo.

## README sempre allineato

`README.md` descrive moduli, comandi, output e comportamento visibile del sistema. Ogni
modifica importante lo aggiorna nello stesso commit, senza aspettare che qualcuno lo chieda.
E' importante una modifica che cambia qualcosa che il README racconta:

- un modulo aggiunto, rimosso, rinominato o con responsabilita' diverse: sezione "I moduli
  nel dettaglio", e la tabella "Struttura del pacchetto" qui sopra;
- un comando, uno script, un'opzione da riga di comando o una variabile d'ambiente: "Come si
  usa" e "Configurazione";
- cio' che l'utente vede: righe in console, report e file in `reports/`, messaggi Telegram,
  codici di uscita, output di `scripts/status.py`: "Come si usa" e "Dove finisce ogni cosa";
- job o orari dello scheduler, limiti del rischio in live, universo o parametri di default
  della strategia, campi e default di `BacktestConfig`: le sezioni che li riportano;
- regole di dominio, fasi del loop, differenze fra backtest e live: "Architettura".

Refactoring interni, correzioni che non cambiano il comportamento e nuovi test non lo
richiedono. Prima di chiudere un lavoro si rilegge la parte di README toccata dalla modifica
e si verifica che dica ancora il vero; se la modifica riguarda anche una guida in `docs/`,
si aggiorna pure quella. Nel messaggio finale si dice quali sezioni del README sono cambiate,
oppure che non serviva cambiarle. Lo stile resta quello del file: italiano, apostrofo al
posto delle lettere accentate, esempi di output nel formato che il codice produce davvero.

## Dati di qualita' produzione (Fase 5)

Il fornitore predefinito e' Tiingo, chiave in `TIINGO_API_KEY`; yfinance resta come secondo
parere con `--source yfinance`. Guida completa in `docs/data.md`.

- **Contratto unico in ingresso.** `DataSource.fetch` restituisce prezzi grezzi piu'
  `dividend` e `split`, verificati da `validate_schema`. Cambiare fornitore vuol dire
  scrivere una sottoclasse, non toccare il download.
- **Controlli prima della scrittura.** Il download scarica tutti i simboli, costruisce il
  calendario, controlla e solo allora scrive: un `BLOCKING` non lascia Parquet parziali, il
  report `reports/data_quality_AAAA-MM-GG.md` si scrive comunque, e con un blocco si esce
  con codice diverso da zero e parte l'alert Telegram.
- **Calendario di borsa.** Quello Alpaca, lo stesso del live; senza chiavi o senza rete si
  ripiega sull'unione delle date dei simboli scaricati, e il report lo dichiara.
- **Manifest come guardia.** `data/manifest.json` e' versionato in git e va committato dopo
  ogni download. `--update` si rifiuta di accodare se la voce manca, se il fornitore
  registrato e' un altro o se il Parquet non corrisponde piu' al suo hash.
- **Provenienza.** `run_backtest` restituisce l'impronta di dati, commit, config e
  strategia, e i report la stampano in coda. Dice se due risultati sono confrontabili, non
  li rende tali.
- **La deriva si segnala, non si corregge.** `--check` riscarica e confronta i soli valori
  grezzi, senza scrivere: le differenze vanno in `reports/data_drift_AAAA-MM-GG.csv` con lo
  SHA-256 del Parquet, per risalire ai backtest che quelle barre le hanno gia' usate.

## Live (Fase 3)

`scripts/live.py` avvia lo scheduler APScheduler: run alle 15:45 ET e riconciliazione alle
09:35 ET nei giorni di borsa, report settimanale il lunedi' alle 08:00 ET, tutti sul fuso di
New York e con le festivita' prese dal calendario Alpaca.
`quant/live/runner.py` esegue una passata: stato, barre, segnali, rischio, esecuzione,
persistenza, riconciliazione. `quant/brokers/` contiene gli adapter Alpaca, `quant/state.py`
lo stato su SQLite. Guida operativa completa in `docs/live.md`.

- **Nessuna sottoclasse live.** Strategy, Portfolio e RiskManager sono le stesse classi del
  backtest. Cambia solo l'ExecutionHandler, che e' il punto in cui il backtest e il broker
  divergono per natura.
- **Storia rigiocata.** Il runner ripercorre tutto lo storico caricato e usa solo i segnali
  dell'ultima barra: cosi' lo stato interno della strategia si ricostruisce da solo dopo un
  riavvio e non serve persisterlo.
- **Idempotenza.** `client_order_id` deterministico da giornata, simbolo e strategia. Al
  massimo un ordine al giorno per simbolo per strategia.
- **Ordini pendenti nel database.** L'handler viene ricreato a ogni esecuzione, quindi gli
  ordini di cui si aspetta l'esito si rileggono da SQLite, non dalla memoria.
- **Fallimento.** Un'eccezione in `run_once` attiva il kill switch e non viene mai ritentata
  in automatico.

## Confronto live contro backtest (Fase 4)

`quant/shadow.py` rigioca la strategia sui Parquet definitivi dal giorno di avvio del live.
`quant/reconcile_report.py` appaia gli eseguiti reali e teorici per giornata e simbolo e
misura scarti, costi e tracking error. `quant/weekly.py` con `scripts/weekly_report.py`
produce il markdown settimanale con grafico. Guida di lettura in `docs/operations.md`.

- **Sola lettura.** Nessuno strumento di analisi scrive sullo StateStore ne' invia ordini:
  produce solo file dentro `reports/`.
- **Riferimento comune.** Lo slippage dei due lati si misura contro la stessa apertura
  ufficiale, con segno positivo quando l'esecuzione e' sfavorevole.
- **Log su file.** Le decisioni del rischio e le anomalie arrivano dal log JSON che lo
  scheduler scrive da solo in `logs/live.jsonl`, percorso sovrascrivibile con
  `QUANT_LOG_FILE`. Senza quella configurazione i log vanno in console in forma leggibile,
  che il report non sa rileggere.
- **Regola sui parametri.** Dal confronto si aggiornano solo i costi del backtest, mai i
  parametri della strategia: il periodo live e' l'unico dato fuori campione rimasto.

## Interfaccia (Fase 6)

`scripts/ui.py` avvia una dashboard FastAPI su `127.0.0.1:8000`: stato live, performance
contro shadow e SPY, ordini ed eseguiti, decisioni del rischio, report. `quant/webui/`
contiene strato dati, avvisi, grafici e template. Guida in `docs/ui.md`.

- **Sola lettura imposta, non promessa.** Lo StateStore si apre con `StateStore.read_only`,
  URI SQLite `mode=ro`: una scrittura solleva nel driver, un database assente non viene
  creato. Nessun modulo della UI importa i broker, tutte le rotte sono GET, niente cookie ne'
  sessioni, il file `KILL` si legge soltanto. L'avvio toglie `QUANT_LOG_FILE` prima di
  creare logger, cosi' la dashboard non scrive nel log del live.
- **Solo loopback.** Senza autenticazione un indirizzo raggiungibile da fuori esporrebbe
  posizioni e ordini: `--host` accetta solo `127.0.0.x` o `localhost` ed esce con codice 2
  spiegando perche'. L'header Host e' controllato contro il DNS rebinding. Da un altro
  computer si passa da un tunnel SSH.
- **Nessun calcolo nella UI.** I numeri vengono da `analysis`, `reconcile_report`, `weekly`,
  `logreader` e `state`; quello che mancava e' stato aggiunto li' con i suoi test, per esempio
  `confronto_live`, `drawdown_series`, `real_fill_events`, `decisions_by_reason`.
- **Shadow in memoria.** Performance e Ordini rigiocano lo shadow con `weekly.shadow_live` e
  lo tengono finche' non cambiano giornata o Parquet: la pagina si ricarica ogni minuto, il
  backtest costa secondi. Nei test lo shadow si inietta in `create_app`.
- **Niente JavaScript.** Grafici matplotlib senza pyplot esportati in SVG inline, con i
  colori come variabili CSS per il tema chiaro e scuro; aggiornamento con `meta refresh`;
  una Content-Security-Policy che non carica niente da fuori.

## Qualita'

    uv run pytest          # suite offline, test property-based inclusi
    uv run ruff check .    # line-length 110, regole E F I UP B
    uv run mypy quant      # strict su tutto il pacchetto

Gli stessi tre comandi girano in CI su ogni push e pull request, in
`.github/workflows/test.yml`. `tests/test_no_lookahead.py` usa hypothesis per generare
calendari con buchi casuali e verifica che nessuna barra oltre il cursore sia visibile.
Durante i test il log applicativo e' a WARNING, impostato in `tests/conftest.py`.

## Stato

Fasi 1, 2, 2.5, 3, 4, 5 e 6 complete. Gli script di ricerca stanno in `quant/research/`, gli
output in `reports/`, che non e' versionato. Il confronto usa benchmark ribilanciati
mensilmente: senza ribilanciamento le cedole resterebbero ferme in cassa e il paragone
favorirebbe la strategia attiva.
