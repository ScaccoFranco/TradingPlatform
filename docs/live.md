# Paper trading: guida operativa

Il live usa le stesse classi del backtest. Cambiano solo due cose: le barre arrivano
dall'API di Alpaca invece che dai Parquet, e gli ordini vanno al broker invece che
all'esecuzione simulata. Strategia, portafoglio e gestore del rischio sono identici.

## Configurazione

Copiare `.env.example` in `.env` e riempirlo con le chiavi del paper trading, che si
generano dalla dashboard Alpaca. Il file `.env` e' in `.gitignore` e non va mai committato.

    ALPACA_API_KEY=...
    ALPACA_SECRET_KEY=...
    ALPACA_PAPER=true

Con `ALPACA_PAPER` diverso da `true` il processo si rifiuta di partire: il trading con
denaro reale non e' supportato da questo codice. Le due variabili Telegram sono
facoltative; se mancano gli alert vengono solo scritti nel log.

## Avvio

    uv run python scripts/live.py

Lo scheduler resta in primo piano e registra due job, entrambi sul fuso di New York e
mai sull'ora locale della macchina:

| Job | Orario | Cosa fa |
|---|---|---|
| trading | 15:45 ET, lun-ven | Scarica le barre, genera i segnali, invia gli ordini |
| riconciliazione | 09:35 ET, lun-ven | Confronta posizioni attese e reali, non invia nulla |

Le festivita' vengono escluse a ogni esecuzione interrogando il calendario di Alpaca.
Se il calendario non risponde si ripiega sui giorni feriali e lo si scrive nel log.

## Log

Lo scheduler scrive i log in JSON nel file `logs/live.jsonl`, una riga per evento, con
un `run_id` che lega fra loro tutte le fasi della stessa esecuzione. Il percorso si cambia
con la variabile d'ambiente `QUANT_LOG_FILE`, il livello con `QUANT_LOG_LEVEL`. Il formato
JSON non e' una preferenza: e' quello che il report settimanale rilegge per trovare le
decisioni del rischio e le anomalie. Per seguirli conviene un filtro:

    tail -f logs/live.jsonl | jq -c 'select(.level != "info")'

Eventi da tenere d'occhio: `ordine_inviato`, `fill_ricevuto`, `ordine_rifiutato`,
`ordine_ridotto`, `RECONCILIATION_MISMATCH`, `kill_switch_attivato`, `run_fallita`,
`posizione_senza_barre`.

Per una fotografia dello stato senza leggere i log:

    uv run python scripts/status.py

## Kill switch

Il blocco e' il file `KILL` nella root del progetto. Finche' esiste, il gestore del
rischio rifiuta ogni ordine, comprese le chiusure di posizione.

    echo "motivo del blocco" > KILL   # attiva
    rm KILL                           # disattiva

Il sistema lo attiva da solo quando `run_once()` solleva un'eccezione. In quel caso
non viene fatto nessun tentativo automatico: si legge il traceback, si capisce cosa e'
successo, si sistema, poi si rimuove il file a mano. Un riavvio che ritenta da solo su
uno stato che non si e' capito e' il modo piu' rapido per moltiplicare il danno.

## RECONCILIATION_MISMATCH

Significa che le posizioni attese, ricostruite da `data/live.db`, non coincidono con
quelle che il broker riporta davvero. Il sistema lo scrive nel log e manda l'alert, ma
non corregge niente di sua iniziativa.

Cosa fare, nell'ordine:

1. Attivare il kill switch, prima di ogni altra cosa.
2. Guardare il conto su Alpaca e confrontarlo con `uv run python scripts/status.py`.
3. Cercare la causa: un ordine eseguito parzialmente, un ordine chiuso a fine giornata,
   un fill arrivato dopo l'ultima esecuzione, o un intervento manuale sul conto.
4. Decidere a mano quale delle due fotografie e' quella giusta. Se lo e' il broker,
   allineare lo stato salvato; se lo sono le posizioni attese, sistemare il conto.
5. Rimuovere il file `KILL` solo quando i due numeri coincidono di nuovo.

## Idempotenza

Ogni ordine porta un `client_order_id` deterministico, calcolato da giornata, simbolo e
nome della strategia. Un riavvio nello stesso giorno non duplica gli ordini: il broker
riconosce l'identificativo e il secondo invio viene scartato. La conseguenza voluta e'
che per una strategia esiste al massimo un ordine al giorno per simbolo.
