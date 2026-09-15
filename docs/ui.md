# Dashboard: guardare il live senza poterlo toccare

La dashboard mostra in una pagina web quello che finora si leggeva da `scripts/status.py`,
dal report settimanale e dal log: stato live, performance contro shadow e mercato, ordini,
decisioni del rischio, report. Legge soltanto. Non ha un solo pulsante che agisca sul
sistema, e non lo avra' finche' non c'e' un'autenticazione.

## Avvio

    uv run python scripts/ui.py
    uv run python scripts/ui.py --port 8010 --log /var/log/quant/live.jsonl

In console compare una riga sola:

    Dashboard di sola lettura su http://127.0.0.1:8000/  (Ctrl+C per fermarla)

| Opzione | Default | Cosa cambia |
|---|---|---|
| `--host` | `127.0.0.1` | Indirizzo di ascolto: solo `127.0.0.x` o `localhost` |
| `--port` | `8000` | Porta |
| `--db` | `data/live.db` | StateStore del live |
| `--data` | `data/parquet` | Parquet: calendario di borsa, aperture ufficiali, SPY, SHY, shadow |
| `--reports` | `reports` | Cartella dei report da elencare |
| `--log` | `QUANT_LOG_FILE`, poi `logs/live.jsonl` | Log JSON scritto dallo scheduler |

Un host che non sia di loopback fa uscire con codice 2, prima che il server parta:

    Avvio rifiutato: 0.0.0.0 non e' un indirizzo di loopback. La dashboard non ha autenticazione: in ascolto su un indirizzo raggiungibile da fuori, chiunque arrivi alla macchina vedrebbe posizioni, equity e ordini. Per guardarla da un altro computer si usa un tunnel SSH verso 127.0.0.1, per esempio ssh -L 8000:127.0.0.1:8000 utente@macchina.

Le pagine si ricaricano da sole ogni 60 secondi con un `meta refresh`. Non serve JavaScript
e non si scarica niente da internet: CSS e grafici sono dentro la pagina o serviti dal
processo stesso.

## Gli avvisi in cima a ogni pagina

Un riquadro rosso dice cosa e' successo e dove guardare. Uno blu e' una nota, non un guaio.

| Avviso | Quando | Dove guardare |
|---|---|---|
| Kill switch attivo | Esiste il file `KILL` | Il motivo scritto nel file e le righe `run_fallita` del log |
| Database live non leggibile | `data/live.db` c'e' ma SQLite non lo apre | Il file stesso e l'ultimo `run_fallita` |
| Nessuna esecuzione da N giornate di borsa | Piu' di due sedute dall'ultima esecuzione nota | Che `scripts/live.py` sia in esecuzione, e la coda del log |
| Esecuzioni fallite | `run_fallita` negli ultimi 7 giorni | Il log, con il `run_id` indicato: lo stack trace e' nel campo `exception` |
| RECONCILIATION_MISMATCH | Disallineamenti negli ultimi 7 giorni | Posizioni attese contro broker |
| Nessuna esecuzione live registrata (nota) | Il database non esiste ancora | Normale prima del primo run |
| Esecuzioni non visibili nel log (nota) | Log assente o senza passate | `--log` o `QUANT_LOG_FILE` |

L'ultima esecuzione e' la data piu' recente fra l'ultima passata nel log e l'ultima equity
salvata, cosi' un log finito altrove non fa sembrare fermo un runner che gira. Le sedute si
contano sul calendario di SPY nei Parquet, festivita' comprese; oltre l'ultima barra salvata
si contano i giorni feriali. "Oggi" e' la data di New York.

## Stato

Equity, cassa e valore delle posizioni dell'ultima rilevazione, lo stato del kill switch,
l'ultima esecuzione con `run_id` ed esito, le posizioni attese e gli ultimi dieci ordini. In
fondo, i percorsi da cui la pagina ha letto. Sono gli stessi numeri di `scripts/status.py`.

## Performance

Tre grafici e due tabelle sul periodo dall'avvio del live.

- **Equity**: live, shadow e SPY a base 100 dal primo giorno di ciascuno, in scala
  logaritmica. Conta la forma, non il livello.
- **Drawdown**: distanza dal massimo precedente, giorno per giorno, per le tre serie.
- **Esposizione del live**: valore delle posizioni in frazione dell'equity, dallo stato
  salvato. Per un portafoglio solo long coincide con l'esposizione lorda. Lo shadow non
  registra le posizioni giorno per giorno, quindi non compare.
- **Tracking error**: cumulato, cioe' rendimento live meno rendimento shadow, e giornaliero,
  deviazione standard della differenza dei rendimenti. Sono le stesse due misure del report
  settimanale.
- **Metriche**: CAGR, volatilita', Sharpe e Sortino in eccesso su SHY, max drawdown, numero di
  trade, commissioni e slippage, tutte da `compute_metrics` con lo stesso tasso per le tre
  colonne. Lo slippage live e' misurato contro l'apertura ufficiale, quello dello shadow e' il
  simulato; positivo vuol dire a sfavore. SPY non ha trade ne' costi e mostra un trattino.

Su poche settimane CAGR e rapporti annualizzati oscillano molto: il numero da guardare
all'inizio e' il tracking error.

Lo shadow si rigioca dentro il processo della dashboard, con la strategia e l'universo di
`quant/live/deployment.py`, e resta in memoria finche' non cambiano la giornata o i Parquet.
La prima apertura della pagina dopo un `--update` impiega qualche secondo. Se il backtest
fallisce, per esempio perche' manca un Parquet dell'universo, la pagina lo dice e mostra
live e SPY da soli.

## Ordini

Filtro per giornate e simbolo nella query string, per default gli ultimi 30 giorni:

    http://127.0.0.1:8000/ordini?dal=2026-09-01&al=2026-09-11&simbolo=SPY

- **In attesa di esito**: ordini senza eseguito e non ancora chiusi, su tutto lo storico. Se
  il numero non torna a zero dopo l'apertura, il broker non ha risposto o l'eseguito non e'
  stato registrato.
- **Ordini**: stato colorato per famiglia, verde se eseguito, rosso se chiuso senza esecuzione
  (`canceled`, `expired`, `rejected`), blu se ancora aperto.
- **Eseguiti**: per ognuno lo slippage sull'apertura ufficiale e lo scarto in punti base dal
  fill teorico dello shadow della stessa giornata, quando lo shadow ne ha uno. E' lo stesso
  appaiamento del report settimanale.
- **Eseguiti teorici senza corrispettivo live**: lo shadow ha operato, il live no.

Un valore del filtro che non si legge viene ignorato e la pagina lo dice.

## Rischio

Finestra di 1, 7, 30 o 90 giorni, sulle giornate UTC del log.

- **Anomalie**, in evidenza: `RECONCILIATION_MISMATCH`, `kill_switch_attivato`,
  `posizione_senza_barre` ed esecuzioni fallite, con i campi che il log riporta.
- **Decisioni del gestore del rischio**: rifiutate e ridotte contate a parte, poi raggruppate
  per motivo di `RiskReason`. Il RiskManager unisce piu' motivi con `+`: un ordine ridotto per
  peso e per controvalore compare in entrambi i gruppi. Le approvazioni pure sono loggate a
  DEBUG e in live di solito non compaiono.

Una finestra senza decisioni lo dice esplicitamente, non mostra una tabella vuota.

## Report

L'elenco dei file in `reports/`, dal piu' recente. Markdown, CSV e testo si aprono come testo
in un blocco a larghezza fissa; il PNG che accompagna un report settimanale compare sopra il
testo. I PNG si aprono anche da soli. Gli altri formati si elencano senza link.

Il percorso richiesto si risolve, link simbolici compresi, e deve restare dentro la cartella
dei report: `..`, percorsi assoluti, file nascosti e link che puntano fuori rispondono 404.

## Cosa la dashboard non puo' fare

- Non invia, modifica ne' annulla ordini: non importa i moduli dei broker e non crea client
  Alpaca.
- Non legge le chiavi API.
- Non ferma il live e non lancia esecuzioni.
- Non tocca il kill switch. Resta il file `KILL` nella radice del progetto: si attiva
  creandolo, con il motivo dentro, e si disattiva cancellandolo, da riga di comando.
- Non scrive sullo StateStore, sui Parquet, nei report o nel log del live.
- Non accede alla rete: niente Alpaca, niente yfinance, niente CDN.

## Dove e' imposta la sola lettura

| Dove | Come |
|---|---|
| SQLite | `StateStore.read_only` apre il database con l'URI `mode=ro`: una scrittura solleva nel driver. Se il file non c'e', non viene creato |
| Import | Nessun modulo di `quant/webui/` importa `quant.brokers` o `alpaca`; un test lo verifica in un processo pulito |
| Rotte | Solo GET. Niente cookie, sessioni o documentazione interattiva |
| Rete | Ascolto solo su loopback; l'header Host deve essere `127.0.0.1` o `localhost`, contro il DNS rebinding |
| Browser | Una Content-Security-Policy impedisce di caricare script o risorse esterne |
| Log | L'avvio toglie `QUANT_LOG_FILE` prima di creare logger: le righe della dashboard vanno in console |
