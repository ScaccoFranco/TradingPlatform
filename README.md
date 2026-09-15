# quant - motore di backtesting event-driven

Backtest e paper trading di strategie su ETF con barre daily. Lo stesso codice di strategia,
portafoglio e gestione del rischio gira sia sullo storico sia sul conto paper di Alpaca;
cambiano solo da dove arrivano le barre e chi esegue gli ordini.

Il sistema e' costruito perche' il look-ahead sia impossibile per struttura, non perche'
qualcuno lo controlli dopo: la strategia vede solo le barre fino al cursore, e un ordine
deciso sulla barra T viene eseguito all'apertura della barra T+1. Accanto al motore ci sono
una catena dati verificabile (Tiingo, controlli di qualita', manifest con SHA-256), gli
strumenti di validazione (walk-forward, sensitivita', Deflated Sharpe) e il confronto
continuo fra il live e un backtest rigiocato sugli stessi giorni, che si guarda anche da una
dashboard locale di sola lettura.

Stato: fasi 1, 2, 2.5, 3, 4, 5 e 6 complete. Architettura, regole di dominio e scelte di
progetto sono in [CLAUDE.md](CLAUDE.md); le guide operative in [docs/](docs/).

## Indice

- [Avvio rapido](#avvio-rapido)
- [Configurazione](#configurazione)
- [Come si usa](#come-si-usa): comandi, output, alert, file prodotti
- [Architettura](#architettura)
- [I moduli nel dettaglio](#i-moduli-nel-dettaglio)
- [La strategia momentum](#la-strategia-momentum)
- [Test e qualita'](#test-e-qualita)
- [Documentazione](#documentazione)

## Avvio rapido

Servono Python 3.12 e [uv](https://docs.astral.sh/uv/).

    uv sync                                          # ambiente virtuale e dipendenze
    cp .env.example .env                             # poi inserire almeno TIINGO_API_KEY
    uv run python scripts/download.py                # storico completo dal 2005, 14 simboli
    git add data/manifest.json && git commit -m "dati"   # il manifest si versiona sempre
    uv run python -m quant.research.buy_and_hold     # primo backtest, stampa una tabella
    uv run python -m quant.research.insample         # analisi 2005-2018 con grafico

Per il paper trading servono anche le chiavi Alpaca, poi:

    uv run python scripts/live.py                    # scheduler in primo piano
    uv run python scripts/status.py                  # in un altro terminale, quando serve
    uv run python scripts/ui.py                      # oppure nel browser, http://127.0.0.1:8000

## Configurazione

Tutti i segreti stanno in `.env`, che non e' versionato; `.env.example` e' il modello.

| Variabile | Serve a | Obbligatoria |
|---|---|---|
| `TIINGO_API_KEY` | `scripts/download.py` con la sorgente predefinita | per scaricare dati da Tiingo |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | live, calendario di borsa nei controlli dati | per il live |
| `ALPACA_PAPER` | deve valere `true`: con altro valore il live rifiuta di partire | si', resta `true` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | alert su Telegram | no: senza, gli alert finiscono solo nel log |
| `QUANT_LOG_FILE` | percorso del log JSON, default `logs/live.jsonl` in live; la dashboard lo legge ma non ci scrive | no |
| `QUANT_LOG_LEVEL` | livello di log (`DEBUG`, `INFO`, `WARNING`, ...) | no |

Il piano gratuito di Tiingo basta per l'universo attuale: un download completo sono 14
richieste. Senza chiavi Alpaca il download funziona lo stesso, ma il calendario di borsa
ripiega sull'unione delle date dei simboli scaricati, e il report lo dichiara.

I parametri dei backtest non passano dall'ambiente ma da `BacktestConfig` in
`quant/config.py`; i limiti del live stanno in `scripts/live.py`, universo e strategia del
live in `quant/live/deployment.py`.

## Come si usa

Il sistema si comanda da riga di comando e comunica attraverso cinque canali:

1. **La console**: ogni comando stampa una riga per simbolo o una tabella di sintesi.
2. **I file in `reports/`**: report markdown, grafici PNG e CSV, scritti a ogni esecuzione.
   La cartella non e' versionata.
3. **Gli alert Telegram**, se configurati: messaggi brevi nel formato
   `[quant][LIVELLO] testo`, pensati per accorgersi di un problema senza guardare il terminale.
4. **Il codice di uscita**: ogni comando dei dati esce con 1 quando qualcosa non torna,
   quindi si puo' mettere in cron.
5. **La dashboard locale**, `scripts/ui.py`: pagine web di sola lettura su 127.0.0.1 con
   stato, performance, ordini, rischio e report. Mostra e non comanda: nessun pulsante agisce
   sul sistema.

Il sistema si ferma con il file `KILL` nella radice del progetto. Nessun comando richiede
interazione: nessun prompt, nessuna conferma.

### Il flusso di lavoro tipico

    Ricerca                              Operativita' (paper trading)
    -------                              ----------------------------
    download.py            (una volta)   live.py            (sempre acceso)
      -> commit del manifest               -> 09:35 ET riconciliazione
    research.insample      (sviluppo)      -> 15:45 ET segnali e ordini
    research.dividend_accounting           -> lun 08:00 ET report settimanale
    research.oos           (una volta)   download.py --update   (ogni sera)
                                         status.py              (a richiesta)
                                         ui.py                  (a richiesta)
                                         download.py --check    (periodicamente)

### Dati: `scripts/download.py`

    uv run python scripts/download.py [SIMBOLI] [--update | --check] [--source tiingo|yfinance] [--start DATA]

| Modalita' | Cosa fa | Scrive |
|---|---|---|
| nessuna opzione | Storico completo da `--start` (default 2005-01-01) | Parquet, manifest, report di qualita' |
| `--update` | Chiede alla sorgente solo le barre dopo l'ultima salvata | Parquet, manifest, report di qualita' |
| `--check` | Riscarica l'intervallo gia' salvato e lo confronta | Solo il CSV della deriva |
| `--source yfinance` | Usa yfinance invece di Tiingo, con qualunque modalita' | come sopra |

Senza simboli si scarica l'universo completo: i 12 ETF di rischio (SPY, QQQ, IWM, EFA, EEM,
VNQ, TLT, IEF, LQD, HYG, GLD, DBC) piu' SHY e BIL. BIL e' opzionale: se non arriva il
download non fallisce. La seduta di oggi, ora di New York, non viene mai salvata perche'
il suo close e' ancora provvisorio.

Cosa si vede in console (esempio):

    sorgente: tiingo
    calendario: alpaca
    SPY: 1 barre nuove, ultima 2026-09-11, 0 bloccanti, 0 avvisi, scritto
    EEM: 1 barre nuove, ultima 2026-09-10, 1 bloccanti, 0 avvisi, NON SCRITTO
    BIL: saltato (BIL: Tiingo non raggiungibile (...))
    report: reports/data_quality_2026-09-12.md

L'ordine di lavoro e' sempre lo stesso: si scarica tutto, si costruisce il calendario, si
controlla, e solo allora si scrive. Un simbolo con un finding `BLOCKING` non viene scritto
e sul disco resta la versione precedente; gli altri simboli si scrivono normalmente. Il
report `reports/data_quality_AAAA-MM-GG.md` si scrive sempre, anche quando va tutto bene:
in testa sorgente, modalita', calendario e soglie, poi una tabella per simbolo (esito,
barre nuove, ultima barra, bloccanti, avvisi, Parquet scritto o no), poi il dettaglio dei
problemi con codice, data e valori.

Gli esiti per simbolo sono `ok`, `avvisi`, `bloccato`, `errore` (sorgente o manifest) e
`saltato` (errore su un simbolo opzionale). Con un blocco o un errore su un simbolo
obbligatorio si esce con 1 e parte un alert Telegram di livello `error`.

Dopo ogni download che scrive, **si committa `data/manifest.json`**: e' l'unica traccia in
git di quali dati esistevano quando. `--update` si rifiuta di accodare se la voce del
simbolo manca dal manifest, se il fornitore registrato e' un altro o se il Parquet non
corrisponde piu' al suo hash; in tutti e tre i casi serve un download completo.

`--check` stampa una riga per simbolo (`SPY: nessuna differenza su 5457 barre (tiingo
contro tiingo)`) e scrive `reports/data_drift_AAAA-MM-GG.csv`, una riga per valore cambiato,
con lo SHA-256 del Parquet confrontato per risalire ai backtest che hanno usato quelle
barre. Con una differenza esce con 1 e manda l'alert. Cosa fare con un `BLOCKING` o con una
deriva e' spiegato passo per passo in [docs/data.md](docs/data.md).

### Ricerca: `python -m quant.research.*`

Gli script di ricerca stampano in console e salvano in `reports/`. Tutti usano gli stessi
parametri di costo (commissione fissa di 1 per trade, slippage di 5 bps, cuscinetto di
cassa dell'1%, capitale 100.000) e confrontano la strategia con due benchmark ribilanciati
mensilmente nello stesso motore: SPY al 100% e un 60/40 SPY/IEF.

| Comando | Periodo | Cosa produce |
|---|---|---|
| `quant.research.buy_and_hold` | SPY dal 2010 | Solo console: le due contabilita' delle cedole a confronto |
| `quant.research.insample` | 2005-2018 | `insample.md`, `insample_equity.png`, `sensitivity.csv` |
| `quant.research.dividend_accounting` | 2006-2018 | `dividend_accounting.md`: cedole in cassa contro prezzi rettificati |
| `quant.research.oos` | 2019-oggi | `oos.md`, `oos_equity.png` |

L'output principale e' una tabella a larghezza fissa, una colonna per serie:

    Metrica                      momentum    SPY mensile  60/40 mensile
    -------------------------------------------------------------------
    Rendimento totale             211.69%        174.59%        146.84%
    CAGR                            8.47%          7.49%          6.67%
    Volatilita' annua              13.18%         18.66%         10.13%
    Sharpe (excess)                  0.53           0.37           0.48
    ...
    Costi totali                   10,373            133            593

Sharpe e Sortino sono calcolati sui rendimenti in eccesso rispetto a SHY (l'etichetta
diventa `Sharpe (rf=0)` quando il tasso privo di rischio non c'e'); CAGR, volatilita' e
drawdown restano lordi. Commissioni e slippage compaiono come righe separate.

`insample` fa anche il confronto con il 2005 usato solo come riscaldamento, una griglia di
sensitivita' su 24 combinazioni (lookback 3/6/9/12 mesi, top 2/3/4, media mobile 100/200
giorni) salvata in `sensitivity.csv` con l'impronta di ogni riga, e il Deflated Sharpe
della configurazione di default, che sconta il numero di prove fatte.

`oos` usa i parametri di default, senza ritocchi: il periodo dal 2019 e' riservato alla
validazione e va lanciato una volta sola. Ogni report termina con una sezione
**Provenienza** per ogni backtest: impronta, commit (con l'avviso se il working tree era
sporco), hash del manifest e dei Parquet letti, finestra, parametri della strategia e config.

### Paper trading: `scripts/live.py`

    uv run python scripts/live.py

All'avvio il processo configura il log JSON su file, verifica che `ALPACA_PAPER` sia `true`
e che le chiavi ci siano, e segnala subito se il file `KILL` e' presente. Poi resta in primo
piano con tre job, tutti sul fuso di New York e mai sull'ora locale:

| Job | Quando | Cosa fa |
|---|---|---|
| riconciliazione | 09:35 ET, giorni di borsa | Confronta le posizioni attese in SQLite con quelle del broker. Non invia nulla |
| trading | 15:45 ET, giorni di borsa | Barre, segnali, rischio, ordini, persistenza, riconciliazione |
| report settimanale | lunedi' 08:00 ET | Genera il report live contro shadow e ne manda la sintesi su Telegram |

Le festivita' si scoprono a ogni esecuzione dal calendario Alpaca; se il calendario non
risponde si ripiega sui giorni feriali e lo si scrive nel log. Con il file `KILL` presente
il job di trading non parte affatto.

Una passata del job di trading, in ordine:

1. ricarica le posizioni attese da `data/live.db` e la cassa dal conto Alpaca;
2. scarica gli ultimi 600 giorni di barre daily da Alpaca e porta il cursore all'ultima;
3. raccoglie gli eseguiti degli ordini inviati in precedenza, anche prima di un riavvio;
4. rigioca la strategia e prende solo i segnali dell'ultima barra;
5. trasforma i segnali in ordini, li fa passare dal gestore del rischio e invia i superstiti
   come ordini a mercato validi per la giornata;
6. salva posizioni ed equity della giornata, poi riconcilia con il broker.

I limiti del live, in `scripts/live.py`: peso massimo del 40% per simbolo, esposizione lorda
al 100%, drawdown massimo del 25%, 8 ordini al giorno, 50.000 di controvalore per ordine, e
solo i 13 simboli dell'universo. Un'eccezione in una passata attiva il kill switch e non
viene mai ritentata in automatico.

Gli alert Telegram che si ricevono:

| Livello | Quando |
|---|---|
| `info` | ordine inviato, fill ricevuto, report settimanale senza anomalie |
| `warning` | ordine ridotto o rifiutato dal rischio (con il motivo), report settimanale con ATTENZIONE |
| `error` | `RECONCILIATION_MISMATCH`, run fallita con kill switch attivato, download bloccato, deriva dei dati |

Il log completo va in `logs/live.jsonl`, una riga JSON per evento, con un `run_id` che lega
le fasi della stessa passata. Per seguirlo:

    tail -f logs/live.jsonl | jq -c 'select(.level != "info")'

Configurazione, kill switch e procedura per un `RECONCILIATION_MISMATCH` sono in
[docs/live.md](docs/live.md).

### Fotografia dello stato: `scripts/status.py`

    uv run python scripts/status.py [PERCORSO_DB]

Legge `data/live.db` (o il percorso indicato) senza modificarlo e stampa, in quest'ordine:
l'eventuale avviso `KILL SWITCH ATTIVO` con il motivo, le posizioni attese, cassa e valore
delle posizioni degli ultimi 30 giorni con la variazione sul periodo, e gli ultimi 10
ordini con stato e `client_order_id`. Esempio:

    Stato live da data/live.db

    Posizioni attese
      GLD           120
      QQQ            55

    Equity degli ultimi 30 giorni
      2026-09-10  cash       812.40  posizioni    99,504.10  totale   100,316.50
      2026-09-11  cash       812.40  posizioni    99,871.25  totale   100,683.65
      variazione sul periodo: +0.37%

    Ultimi 10 ordini
      2026-09-01T00:00:00  GLD    BUY       120    market  filled     quant-3f2a9c01d4e6b7a8

Se il database non esiste ancora lo dice e si ferma.

### Confronto con il backtest: `scripts/weekly_report.py`

    uv run python scripts/weekly_report.py [--giorno AAAA-MM-GG] [--avvio AAAA-MM-GG] [--db PERCORSO] [--log PERCORSO] [--soglia BPS]

| Opzione | Default | Significato |
|---|---|---|
| `--giorno` | oggi | La settimana del report e' quella che contiene questa data |
| `--avvio` | `AVVIO_LIVE` in `quant/live/deployment.py` | Da quando rigiocare lo shadow |
| `--db` | `data/live.db` | StateStore da leggere |
| `--log` | `logs/live.jsonl` | Log JSON da cui leggere decisioni del rischio e anomalie |
| `--soglia` | 50 | Tracking error cumulato in bps oltre cui scatta l'attenzione |

Lo script rigioca la strategia del live sui Parquet definitivi dal giorno di avvio (lo
*shadow backtest*), appaia gli eseguiti reali e teorici per giornata e simbolo, e scrive
`reports/weekly_AAAA-SS.md` con il grafico `weekly_AAAA-SS.png` (settimana ISO). In console
stampa le tre righe di sintesi. E' in sola lettura: non tocca lo stato live e non invia ordini.

Il report e' organizzato per essere letto dall'alto e fermarsi presto:

1. **Blocco ATTENZIONE**, solo se serve: tracking error oltre soglia, slippage reale oltre
   il doppio di quello simulato, eseguiti presenti da un lato solo, mismatch, eccezioni o
   kill switch nei log, giornate di borsa in cui il runner non ha girato.
2. **Sintesi** in tre righe: tracking error, slippage reale contro simulato, conteggi.
3. **Scostamento fra live e shadow**: tracking error giornaliero e cumulato, slippage
   medio dei due lati, commissioni.
4. **Grafico**: live, shadow e SPY a base 100 in scala logaritmica.
5. **Eseguiti della settimana**: quantita' e prezzi dei due lati, scarto in bps
   (positivo quando il reale e' peggiore).
6. **Slippage per simbolo** sull'intero periodo, **decisioni del rischio** e **anomalie**.
7. **Provenienza dello shadow**.

Lo slippage dei due lati si misura contro la stessa apertura ufficiale presa dai Parquet,
quindi i due numeri sono confrontabili. Come interpretarli e quando aggiornare i costi
del backtest e' in [docs/operations.md](docs/operations.md). Dal confronto si aggiornano
solo i costi, mai i parametri della strategia.

### Dashboard: `scripts/ui.py`

    uv run python scripts/ui.py [--host 127.0.0.1] [--port 8000] [--db PERCORSO] [--data PERCORSO] [--reports PERCORSO] [--log PERCORSO]

Avvia un server locale e stampa l'indirizzo:

    Dashboard di sola lettura su http://127.0.0.1:8000/  (Ctrl+C per fermarla)

| Pagina | Cosa mostra |
|---|---|
| `/` Stato | Equity, cassa, kill switch, ultima esecuzione con `run_id`, posizioni attese, ultimi ordini |
| `/performance` | Live, shadow e SPY a base 100 in scala logaritmica, drawdown, esposizione del live, tracking error, metriche di `compute_metrics` |
| `/ordini` | Ordini ed eseguiti filtrati per date e simbolo, con lo scarto in bps dal fill teorico dello shadow |
| `/rischio` | Decisioni del gestore del rischio raggruppate per motivo, anomalie del log in evidenza |
| `/report` | I file di `reports/`: markdown e CSV come testo, PNG come immagini |

In cima a ogni pagina un avviso rosso segnala kill switch attivo, runner fermo da piu' di due
giornate di borsa, esecuzioni fallite o `RECONCILIATION_MISMATCH` degli ultimi sette giorni,
e dice dove guardare. Le pagine si ricaricano ogni 60 secondi, senza JavaScript.

La dashboard legge soltanto: apre SQLite in sola lettura, non importa i broker, non tocca il
file `KILL` e non scrive nel log del live. Ascolta solo su loopback: con un altro `--host`
esce con codice 2 spiegando perche', e da un altro computer la si guarda attraverso un
tunnel SSH. Lettura pagina per pagina in [docs/ui.md](docs/ui.md).

### Kill switch

    echo "motivo del blocco" > KILL      # ferma tutto, sopravvive al riavvio
    rm KILL                              # riattiva, solo dopo aver capito cosa e' successo

Finche' il file esiste il gestore del rischio rifiuta ogni ordine, comprese le chiusure, e
lo scheduler salta il job di trading. Il sistema lo crea da solo quando una passata
fallisce, scrivendoci l'eccezione. La dashboard ne mostra stato e motivo, ma non puo' ne'
crearlo ne' rimuoverlo.

### Dove finisce ogni cosa

| Percorso | Contenuto | In git |
|---|---|---|
| `data/parquet/SIMBOLO.parquet` | Barre daily grezze, cedole, split, `adj_close` e `adj_volume` calcolati in casa | no |
| `data/manifest.json` | Per simbolo: fornitore, data di download, intervallo, barre, SHA-256 | **si'** |
| `data/live.db` | Stato live su SQLite: ordini, fill, posizioni attese, equity giornaliera | no |
| `logs/live.jsonl` | Log JSON dello scheduler | no |
| `KILL` | Kill switch, con il motivo come contenuto | no |
| `reports/data_quality_AAAA-MM-GG.md` | Report di qualita' di ogni download | no |
| `reports/data_drift_AAAA-MM-GG.csv` | Differenze trovate da `--check` | no |
| `reports/insample.md`, `oos.md`, `dividend_accounting.md` | Resoconti della ricerca, con provenienza | no |
| `reports/*_equity.png`, `sensitivity.csv` | Grafici e griglia di sensitivita' | no |
| `reports/weekly_AAAA-SS.md` e `.png` | Report settimanale live contro shadow | no |

La dashboard non aggiunge niente a questo elenco: legge questi file e non ne scrive nessuno.

## Architettura

Un solo loop, in `quant/engine.py`. A ogni passo il data handler avanza il cursore di una
data ed emette un `MarketEvent`; la coda si svuota smistando gli eventi per tipo; si ripete
finche' ci sono barre.

    MarketEvent -> Strategy -> SignalEvent -> Portfolio -> OrderEvent
                -> RiskManager -> ExecutionHandler -> FillEvent -> Portfolio

Su ogni barra l'ordine delle fasi e' fisso:

1. **fill all'apertura**: l'esecuzione riempie gli ordini decisi sulla barra precedente;
2. **operazioni sul capitale e mark-to-market**: il portafoglio applica gli split,
   accredita le cedole e valorizza sui close, aggiungendo un punto all'equity curve;
3. **strategia**: legge le barre fino al cursore ed emette intenzioni, che diventano
   ordini per la barra successiva.

Backtest e live condividono strategia, portafoglio e gestore del rischio. Divergono solo
agli estremi:

| | Backtest | Live |
|---|---|---|
| Barre | `ParquetDataHandler` sui Parquet locali | `AlpacaDataHandler` sull'API storica Alpaca |
| Esecuzione | `SimulatedExecutionHandler`: fill all'open di T+1 con slippage | `AlpacaExecutionHandler`: ordini al conto paper |
| Orchestrazione | `Backtest.run()` su tutta la finestra | `LiveRunner.run_once()` sull'ultima barra, lanciato dallo scheduler |
| Stato | in memoria | SQLite in `data/live.db` |

La dashboard in `quant/webui/` sta fuori dal loop: legge quello che backtest e live hanno
scritto e non partecipa a nessuna delle due catene.

Le regole di dominio non negoziabili (accesso ai dati solo tramite il cursore, esecuzione a
T+1, segnali su `adj_close` e quantita' sui prezzi grezzi, intenzioni e non quantita' dalla
strategia, ogni ordine attraverso il rischio, costi come parametri, `adj_close` calcolato
in casa, nessuna barra sul disco senza controlli) sono elencate in [CLAUDE.md](CLAUDE.md).

## I moduli nel dettaglio

### Nucleo del motore

**`quant/events.py`**: le quattro dataclass frozen scambiate nel loop, senza logica.
`MarketEvent` porta solo il timestamp, non i prezzi: chi li vuole deve chiederli al data
handler. `SignalEvent` ha direzione (`LONG`, `SHORT`, `EXIT`) e forza in [0, 1], mai una
quantita'. `OrderEvent` e' un ordine a mercato gia' dimensionato. `FillEvent` porta prezzo
eseguito, commissione e `slippage_cost` separato, cosi' i costi restano visibili invece di
sparire dentro il prezzo.

**`quant/data.py`**: l'accesso ai dati senza look-ahead. `DataHandler` e' l'interfaccia;
`FrameDataHandler` tiene i DataFrame in memoria, costruisce una timeline unificata come
unione delle date di tutti i simboli e fa avanzare un cursore. `get_latest_bars(symbol, n)`
e `get_latest_bars_all(n)` restituiscono le ultime n barre fino al cursore incluso, con
ricerca binaria e senza forward-fill: un simbolo che non scambia oggi resta semplicemente
alla sua ultima barra. `emitted_timeline()` espone solo le date gia' emesse.
`advance_to_latest()` porta il cursore in fondo, ed e' cosi' che il live lavora sull'ultima
barra. `ParquetDataHandler` legge un file per simbolo, taglia la finestra e puo' applicare
una trasformazione prima del cursore (usata per la vista a prezzi rettificati).

**`quant/strategy.py`**: l'interfaccia `Strategy` con il solo metodo
`on_bar(event) -> list[SignalEvent]`. Il data handler si puo' collegare dopo la
costruzione, cosi' la validazione ricrea la stessa strategia su finestre diverse. Contiene
due strategie di servizio: `NoOpStrategy` (nessun segnale) e `BuyAndHoldStrategy` (un LONG
alla prima barra utile).

**`quant/calendar.py`**: `is_first_trading_day_of_month` decide se la barra corrente e' la
prima del mese guardando solo la barra precedente, gia' emessa. E' l'unico trigger di
ribilanciamento valido in live. `is_last_trading_day_of_month` esiste per l'analisi ma e'
un'approssimazione sui giorni feriali, perche' l'ultimo giorno vero dipende dalle
festivita' future.

**`quant/engine.py`**: la classe `Backtest`, che collega i cinque componenti, gestisce la
coda (`deque`) e smista ogni evento con un `match` sul tipo. Conta gli eventi di ogni tipo
e logga ogni fill. I componenti sono descritti da `Protocol` minimi, quindi il loop non
dipende dalle classi concrete.

**`quant/portfolio.py`**: l'unico componente che conosce il capitale.
- *Dimensionamento*: la quantita' bersaglio e' forza x peso massimo x equity, divisa per
  l'ultimo close grezzo; l'ordine e' la differenza dalla posizione attuale. Gli acquisti
  sono limitati dalla cassa disponibile al netto di `cash_buffer`, contando anche la cassa
  che le vendite gia' decise sulla stessa barra libereranno allo stesso open.
- *Operazioni sul capitale*: su ogni barra applica prima gli split (le azioni si
  moltiplicano, la frazione residua si monetizza all'apertura), poi accredita le cedole a
  chi aveva le azioni alla chiusura precedente la data ex, poi valorizza.
- *Proiezione*: `projected_positions()` restituisce le posizioni dopo gli ordini gia'
  emessi sulla barra, cosi' il rischio valuta una rotazione completa per quello che e' e
  non come un raddoppio.
- *Posizioni ferme*: se un simbolo in portafoglio non ha barre da piu' di 5 giorni lo
  segnala una volta con `posizione_senza_barre` e continua a valorizzarlo all'ultimo prezzo.
- Tiene `equity_curve`, `peak_equity`, `fills` e `dividends_received`.

**`quant/risk.py`**: il `RiskManager` applica i limiti a ogni ordine e restituisce una
`RiskDecision` (ordine originale, ordine finale o `None`, motivo), tenuta in `decisions` e
loggata come `ordine_approvato`, `ordine_ridotto` o `ordine_rifiutato`. La catena dei
controlli, nell'ordine:

1. kill switch, whitelist dei simboli, numero massimo di ordini al giorno: valgono per
   qualunque ordine;
2. prezzo ed equity validi;
3. solo per chi aumenta il rischio: drawdown oltre soglia (rifiuto), peso per simbolo
   (riduzione), esposizione lorda calcolata sulla proiezione post-ordini (rifiuto);
4. controvalore massimo per ordine (riduzione), poi quantita' nulla (rifiuto).

Le riduzioni di posizione passano sempre i limiti di drawdown ed esposizione. I motivi sono
i valori di `RiskReason`. `KillSwitch` e' attivo se esiste il file `KILL` o se e' stato
attivato in memoria; `activate()` scrive il motivo nel file, che sopravvive al riavvio.

**`quant/execution.py`**: `SimulatedExecutionHandler` accoda gli ordini e li riempie
all'apertura della barra successiva in cui il simbolo scambia; chi non ha barra resta in
coda. Lo slippage in bps e' sempre a sfavore (chi compra paga di piu', chi vende incassa di
meno) e la commissione e' fissa per trade, entrambi dal costruttore. Se il simbolo fraziona
proprio quel giorno, la quantita' dell'ordine pendente viene riscalata nella nuova scala.

**`quant/config.py`**: `BacktestConfig` raccoglie tutti i parametri di un backtest in un
oggetto immutabile; `with_overrides(**campi)` crea una copia e rifiuta le chiavi
sconosciute, cosi' un refuso non passa inosservato. `DataQualityConfig` tiene le soglie
dei controlli sui dati. `Settings` legge le variabili d'ambiente e `.env` con
pydantic-settings; `validate_live()` blocca l'avvio senza paper trading o senza chiavi.

| Campo di `BacktestConfig` | Default | Significato |
|---|---|---|
| `initial_cash` | 100.000 | Capitale iniziale |
| `commission_per_trade` | 1.0 | Commissione fissa per eseguito |
| `slippage_bps` | 5.0 | Slippage in punti base, sempre sfavorevole |
| `cash_buffer` | 0.01 | Quota di cassa non investita, protegge dai gap all'apertura |
| `dividends_as_cash` | `True` | Cedole in cassa sui grezzi; `False` usa i prezzi rettificati |
| `max_weight_per_symbol`, `max_gross_exposure`, `max_drawdown` | 1.0 | Limiti del RiskManager |
| `risk_free_symbol` | `"SHY"` | Titolo da cui si ricava il tasso privo di rischio |
| `path` | `data/parquet` | Cartella dei Parquet |

### Strategie

**`quant/strategies/momentum.py`**: `CrossSectionalMomentum`, la strategia del live,
descritta [piu' sotto](#la-strategia-momentum).

**`quant/strategies/fixed_weights.py`**: `FixedWeightsStrategy`, un benchmark a pesi
fissi che riporta ogni simbolo al suo peso alla prima barra di ogni mese (o una volta sola
con `rebalance_monthly=False`). Serve a far girare SPY e il 60/40 nello stesso motore e con
gli stessi costi della strategia attiva.

### Catena dei dati

**`quant/sources.py`**: il contratto unico delle barre in ingresso. `DataSource.fetch`
restituisce un DataFrame con indice `date` senza fuso, prezzi e volumi grezzi, `dividend`
e `split` alla data ex, e facoltativamente l'`adj_close` del vendor, tutto in float64.
`validate_schema` ne verifica la forma e `valida_simbolo` rifiuta i ticker che potrebbero
scrivere fuori da `data/parquet` o cambiare l'URL chiamato.
- `TiingoSource`: una richiesta per simbolo; ritenta 429 ed errori 5xx con attesa
  crescente rispettando `Retry-After`, rinuncia oltre i 120 secondi di attesa, e distingue
  chiave rifiutata (401/403) e simbolo sconosciuto (404).
- `YFinanceSource`: yfinance consegna prezzi gia' rettificati per gli split, e
  `smonta_split` li riporta alla scala grezza moltiplicando ogni barra per gli split
  successivi.

Aggiungere un fornitore vuol dire scrivere una sottoclasse, senza toccare il download.

**`quant/download.py`**: l'orchestrazione di `scripts/download.py`. `run_download`
prepara ogni simbolo senza scrivere (storico completo, oppure solo le barre nuove dopo aver
verificato il manifest), costruisce il calendario, esegue i controlli, scrive i Parquet
senza blocchi in modo atomico (file temporaneo e `os.replace`), aggiorna il manifest,
scrive il report e manda l'alert. `in_formato_parquet` passa dal contratto delle sorgenti
allo schema dei Parquet, con `adj_close` e `adj_volume` calcolati da `quant/adjust.py`.

**`quant/data_quality.py`**: i controlli che decidono se una serie puo' finire sul disco.
Ogni controllo produce `Finding` con severita' `BLOCKING` (non si scrive) o `WARNING` (si
scrive e si segnala).

| Codice | Severita' | Cosa rileva |
|---|---|---|
| `DUPLICATE_DATE`, `UNSORTED_INDEX` | BLOCKING | date ripetute o fuori ordine |
| `INVALID_PRICE`, `INVALID_VOLUME` | BLOCKING | prezzi mancanti, nulli o negativi; volumi mancanti o negativi |
| `HIGH_BELOW_LOW` | BLOCKING | `high` minore di `low` |
| `PRICE_JUMP` | BLOCKING | close che salta oltre il 25% senza uno split a spiegarlo |
| `ADJ_MISMATCH` | WARNING o BLOCKING | `adj_close` del vendor lontano dal calcolo in casa oltre lo 0,1% o il 5% |
| `MISSING_DAY`, `EXTRA_DAY` | WARNING | sedute senza barra, barre in giorni di chiusura |
| `OUT_OF_RANGE`, `ZERO_VOLUME` | WARNING | open o close fuori da [low, high], volume zero |
| `SCHEMA` | BLOCKING | frame fuori contratto, generato dal download |

Con un aggiornamento i finding riguardano solo le barre nuove; le vecchie servono da
contesto. Il calendario delle sedute arriva da Alpaca, con ripiego sull'unione delle date
dei simboli.

**`quant/adjust.py`**: l'unico punto che produce `adj_close`, con il metodo standard a
fattore cumulativo all'indietro: l'ultima barra ha fattore 1 e le barre passate sono
espresse nella scala di oggi, con la cedola reinvestita al prezzo ex. `adjust()` calcola
anche `adj_volume`; `adjusted_view()` porta tutti i prezzi sulla scala rettificata e azzera
cedole e split, ed e' la contabilita' alternativa usata solo nel backtest.

**`quant/manifest.py`**: legge e scrive `data/manifest.json` (scrittura atomica, chiavi
ordinate per diff leggibili). `verify()` e' la guardia di `--update`: voce presente, stesso
fornitore, stesso SHA-256. L'hash dei file e' in cache finche' dimensione e data di
modifica non cambiano.

**`quant/drift.py`**: `run_check` riscarica l'intervallo salvato di ogni simbolo e
`compare` lo confronta valore per valore sulle sole colonne grezze, con tolleranza relativa
di 1e-6. Le differenze sono di tre tipi: `changed`, `removed`, `added`.

**`quant/provenance.py`**: `build_provenance` raccoglie l'impronta di un backtest (hash
dei Parquet letti, commit, working tree sporco o no, config, finestra, classe e parametri
della strategia) e ne calcola lo SHA-256. Se un Parquet non corrisponde al manifest lo
segnala in `manifest_mismatch` e `run_backtest` logga `dati_fuori_manifest`.
`provenance_markdown` produce la sezione in coda ai report.

### Analisi e validazione

**`quant/analysis.py`**: `compute_metrics` calcola rendimento totale, CAGR, volatilita',
Sharpe e Sortino in eccesso, Calmar, max drawdown, numero di trade, commissioni, slippage,
costi totali, asimmetria e curtosi (queste ultime servono al Deflated Sharpe).
`format_table` produce la tabella a larghezza fissa degli script di ricerca, con le etichette
e i formati di `metric_label` e `format_metric`, gli stessi della dashboard. `drawdown_series`
da' la distanza dal picco giorno per giorno, di cui `max_drawdown` e' il minimo.

**`quant/validation.py`**: il punto d'ingresso dei backtest.
- `run_backtest(factory, simboli, start, end, config, warmup_start)` monta i cinque
  componenti, esegue e restituisce `metrics`, `equity_curve`, `fills`, `portfolio`,
  `backtest`, `config` e `provenance`. Con `warmup_start` il backtest parte prima, le
  metriche contano solo da `start` e l'equity viene riscalata al capitale iniziale. Gli
  argomenti sciolti al posto di `BacktestConfig` sono deprecati.
- `parameter_sensitivity` gira una griglia di parametri e restituisce un DataFrame con una
  riga per combinazione.
- `walk_forward` sceglie i parametri per Sharpe su una finestra di train (default 5 anni)
  e li applica al test successivo (1 anno), con il test che non esiste ancora mentre si
  sceglie; l'equity out-of-sample concatenata e' in `attrs["equity_oos"]`.
- `deflated_sharpe` ed `expected_max_sharpe` implementano Bailey e Lopez de Prado (2014).

**`quant/research/`**: gli script di ricerca descritti sopra. `common.py` definisce
universo, config di ricerca, il confronto a tre, il grafico e `Resoconto`, che stampa e
salva il markdown con la provenienza in coda.

### Paper trading

**`quant/state.py`**: `StateStore` su SQLite con quattro tabelle: `orders`, `fills`,
`positions` (posizioni attese) ed `equity` (una riga per giornata). L'inserimento di un
ordine e di un fill e' idempotente sulla chiave `client_order_id`; `pending_orders()`
ricostruisce dopo un riavvio gli ordini di cui si attende l'esito; `find_orders` e
`find_fills` filtrano per giornate e simbolo. `StateStore.read_only` apre un database
esistente con l'URI SQLite `mode=ro`, senza creare niente: ogni scrittura solleva nel driver,
ed e' cosi' che lo apre la dashboard. `reconcile()` confronta
posizioni attese e reali e logga ogni differenza come `RECONCILIATION_MISMATCH`, senza
correggere nulla.

**`quant/brokers/alpaca.py`**: `AlpacaExecutionHandler`, la stessa interfaccia
dell'esecuzione simulata. Ogni ordine ha un `client_order_id` deterministico
(`quant-` piu' 16 caratteri di SHA-256 di strategia, simbolo e giornata): prima di inviare
si controlla memoria, database e broker, e un doppione viene ignorato. L'ordine si registra
nel database prima dell'invio. Supporta ordini market e limit (prezzo limite dall'ultimo
close piu' uno scarto in bps); `collect_fills` trasforma in `FillEvent` gli ordini
eseguiti, senza contarli due volte, e chiude quelli annullati, scaduti o respinti.

**`quant/brokers/alpaca_data.py`**: `AlpacaDataHandler`, un `FrameDataHandler` riempito
dall'API storica. Fa due richieste, barre grezze per prezzi e quantita' e barre rettificate
per la sola colonna `adj_close`. L'API non porta le operazioni sul capitale, quindi
`dividends` e `split_factor` restano neutri e le cedole arrivano dalla cassa del broker.

**`quant/live/runner.py`**: `LiveRunner.run_once()` esegue la passata descritta sopra,
lega tutti i log a un `run_id` e restituisce un `RunSummary` (segnali, decisioni, ordini
inviati, fill, mismatch). `reconcile_now()` e' il job del mattino. Su eccezione logga
`run_fallita`, attiva il kill switch, manda l'alert e rilancia.

**`quant/live/schedule.py`**: costruisce il `BlockingScheduler` di APScheduler con i tre
job, trigger cron sul fuso `America/New_York`, un'istanza per job, recupero fino a 10 minuti
per un'esecuzione mancata. `solo_se_borsa_aperta` salta i giorni di chiusura.

**`quant/live/alerts.py`**: `TelegramAlerter` invia `[quant][LIVELLO] testo` e non solleva
mai: un errore di rete finisce nel log, non ferma il trading. `NullAlerter` si usa quando
le variabili Telegram mancano.

**`quant/live/deployment.py`**: cosa gira in live, in un solo posto: universo, simbolo di
cassa, nome della strategia (`momentum_v1`), data di avvio (`AVVIO_LIVE`) e factory della
strategia. Scheduler e report settimanale leggono tutti da qui.

**`quant/logging.py`**: structlog configurato una volta per processo. Con un file di log
(o `QUANT_LOG_FILE`) scrive JSON una riga per evento, altrimenti console leggibile su
standard error. `configure_for_live()` forza il JSON su `logs/live.jsonl`.

### Confronto live contro backtest

**`quant/shadow.py`**: `ShadowBacktest` rigioca la strategia con `run_backtest` dal giorno
di avvio del live, con 400 giorni di riscaldamento e gli stessi costi del backtest. Il
risultato e' uno `ShadowResult` con equity, fill teorici, metriche e provenienza.

**`quant/reconcile_report.py`**: normalizza gli eseguiti reali (dallo StateStore) e
teorici (dallo shadow) in `FillRecord`, li appaia per giornata e simbolo (`appaiato`,
`solo_live`, `solo_shadow`) e calcola scarto di prezzo, slippage contro l'apertura
ufficiale, tracking error giornaliero e cumulato, e una tabella per simbolo. `write_report`
scrive anche un CSV di dettaglio e uno storico cumulativo, ma oggi nessuno script lo chiama.
`real_fill_events` restituisce gli eseguiti reali come `FillEvent` con lo slippage in valuta
contro l'apertura, cosi' `compute_metrics` somma costi confrontabili con quelli dello shadow;
`live_exposure_series` da' il valore delle posizioni in frazione dell'equity.

**`quant/logreader.py`**: legge `logs/live.jsonl` filtrando per giornata ed estrae
decisioni del rischio, mismatch, eccezioni, anomalie e giornate in cui il runner ha concluso
una passata. `last_run` trova l'ultima passata conclusa o fallita; `decisions_by_reason`
raggruppa le decisioni per motivo, e una decisione con piu' motivi uniti da `+` conta in
ogni gruppo. Se il file non esiste restituisce una lista vuota.

**`quant/weekly.py`**: `generate_weekly_report` mette insieme shadow, confronto, log e
grafico nel markdown settimanale; `_avvisi` decide quando aprire con il blocco ATTENZIONE.
`confronto_live` prepara per la dashboard live, shadow e SPY sullo stesso periodo, con
esposizione, drawdown, tracking error e metriche calcolate allo stesso modo. `shadow_live`
rigioca lo shadow del live con universo e avvio di `quant/live/deployment.py`, per il report
e per la dashboard. `giorni_di_borsa_dopo` conta le sedute sul calendario di SPY e, oltre
l'ultima barra salvata, sui giorni feriali.

### Dashboard

**`quant/webui/read.py`**: lo strato dati, tutto in sola lettura. Apre lo StateStore con
`read_only`, legge log e file `KILL` e restituisce dataclass frozen: `stato_live`,
`serie_equity`, `ordini_e_eseguiti`, `rischio`, `performance`, `elenco_report`. Una sorgente
assente o illeggibile diventa un risultato con `disponibile=False` e una nota, mai
un'eccezione. `risolvi_report` accetta solo file dentro la cartella dei report risolta,
link simbolici compresi. `OmbraMemorizzata` rigioca lo shadow solo quando cambiano la
giornata o i Parquet.

**`quant/webui/avvisi.py`**: gli avvisi in cima alle pagine, ciascuno con cosa e' successo
e dove guardare.

**`quant/webui/charts.py`**: grafici matplotlib costruiti senza pyplot e resi come SVG in una
stringa, con i colori come variabili CSS per il tema chiaro e scuro.

**`quant/webui/formato.py`**: come si scrivono numeri, istanti ed etichette; nessun calcolo.

**`quant/webui/app.py`**: `create_app` con le rotte, tutte GET, i template Jinja in
`templates/` e il CSS in `static/`. Controlla l'header Host contro il DNS rebinding e manda
una Content-Security-Policy che non carica niente da fuori.

**`quant/webui/avvio.py`**: `avvia` rifiuta un host che non sia di loopback, toglie
`QUANT_LOG_FILE` perche' la dashboard non scriva nel log del live, e lancia uvicorn.

### Script e cartelle di supporto

`scripts/` contiene involucri sottili senza logica propria: `download.py` chiama
`quant.download.cli`, `live.py` costruisce i componenti del live e avvia lo scheduler,
`status.py` e `weekly_report.py` leggono lo stato e i report, `ui.py` avvia la dashboard.
`ai/prompts/` conserva i prompt con cui sono state specificate le fasi da 1 a 6.

## La strategia momentum

`CrossSectionalMomentum` in `quant/strategies/momentum.py` e' la strategia che gira in live.

- **Universo**: 12 ETF su azioni USA e internazionali, immobiliare, obbligazioni, oro e
  materie prime (SPY, QQQ, IWM, EFA, EEM, VNQ, TLT, IEF, LQD, HYG, GLD, DBC), con SHY come
  cassa.
- **Quando**: solo sulla prima barra di ogni mese.
- **Segnale**: rendimento su `adj_close` fra 12 mesi fa e 1 mese fa; il mese piu' recente
  resta escluso per evitare il reversal di breve. Un simbolo senza storico sufficiente, o
  che ha smesso di scambiare, esce dalla classifica.
- **Allocazione**: i 3 migliori, ciascuno con forza 1/3.
- **Filtro di trend**: se SPY chiude sotto la sua media mobile a 200 giorni, tutto su SHY.
- **Uscite**: prima i segnali `EXIT` per i simboli che non servono piu', poi i `LONG`.

| Parametro | Default |
|---|---|
| `lookback_months` | 12 |
| `skip_months` | 1 |
| `top_n` | 3 |
| `trend_filter_symbol` | `"SPY"` |
| `trend_sma_days` | 200 |
| `cash_symbol` | `"SHY"` |

I parametri non si modificano sulla base dei risultati live: il periodo live e' l'unico
dato fuori campione rimasto. Le ragioni sono in [docs/operations.md](docs/operations.md).

## Test e qualita'

    uv run pytest          # suite offline, test property-based inclusi
    uv run ruff check .    # line-length 110, regole E F I UP B
    uv run mypy quant      # strict su tutto il pacchetto

Gli stessi tre comandi girano in CI su ogni push e pull request
(`.github/workflows/test.yml`). I test non toccano la rete: usano Parquet sintetici
costruiti nelle fixture (`tests/conftest.py`, `tests/sintetici.py`) e un client Alpaca
finto (`tests/fake_alpaca.py`), e isolano l'ambiente dalle chiavi vere e dal `.env`.
`tests/test_no_lookahead.py` usa hypothesis per generare calendari con buchi casuali e
verifica che nessuna barra oltre il cursore sia mai visibile. La dashboard si prova con il
`TestClient` di FastAPI su StateStore e log sintetici; i test verificano anche che la
connessione SQLite rifiuti le scritture, che nessun percorso esca da `reports/` e che il
processo non importi i broker.

I dati dal 2019-01-01 in poi sono riservati alla validazione out-of-sample: non si usano
nei test ne' negli script di sviluppo.

## Documentazione

| Documento | Contenuto |
|---|---|
| [CLAUDE.md](CLAUDE.md) | Architettura, regole di dominio, scelte di progetto, convenzioni |
| [docs/data.md](docs/data.md) | Fornitore, controlli di qualita', cosa fare con un BLOCKING, manifest, deriva |
| [docs/live.md](docs/live.md) | Configurazione del paper trading, log, kill switch, mismatch, idempotenza |
| [docs/operations.md](docs/operations.md) | Come leggere il report settimanale e quando aggiornare i costi |
| [docs/ui.md](docs/ui.md) | La dashboard: avvio, cosa mostra ogni pagina, cosa non puo' fare |

Questo README descrive moduli, comandi e output: va aggiornato insieme a ogni modifica che
li cambia, come stabilito in [CLAUDE.md](CLAUDE.md).
