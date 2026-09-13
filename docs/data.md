# Dati: fornitore, controlli, riproducibilita'

I prezzi sono l'unico ingresso del sistema che non si puo' verificare leggendo il codice.
Questa catena li rende verificabili: una sorgente dichiarata, controlli che bloccano la
scrittura quando i dati sono rotti, `adj_close` calcolato in casa e un manifest che dice
esattamente quali barre hanno prodotto un risultato.

## Comandi

    uv run python scripts/download.py                          # storico completo da Tiingo
    uv run python scripts/download.py --update                 # solo le barre nuove
    uv run python scripts/download.py --check                  # verifica, senza scrivere
    uv run python scripts/download.py --source yfinance SPY    # un simbolo, altro fornitore

Ogni comando esce con codice diverso da zero se qualcosa non torna, e manda un alert
Telegram se e' configurato: si puo' mettere in cron e accorgersene solo quando serve.

## Configurazione del fornitore

Il fornitore predefinito e' Tiingo. La chiave si mette in `.env`, che non e' versionato:

    TIINGO_API_KEY=...

Si ottiene da <https://www.tiingo.com/account/api/token>. Il piano gratuito basta per
l'universo attuale: 50 richieste l'ora, 1.000 al giorno, 500 simboli diversi al mese, con
oltre trent'anni di storico EOD. Un download completo dei 14 simboli sono 14 richieste,
`--check` altrettante. I dati del piano gratuito sono per uso interno e non si
ridistribuiscono. `yfinance` resta disponibile con `--source yfinance`: non chiede chiavi,
non garantisce prezzi stabili, e serve soprattutto come secondo parere.

## Cosa finisce nei Parquet

Un file per simbolo in `data/parquet/`, indice `date`, prezzi **grezzi** come li consegna
il broker, piu' `dividends`, `split_factor`, `adj_close` e `adj_volume`. Le prime due
colonne sono le operazioni sul capitale alla data ex; le ultime due le calcola
`quant/adjust.py` dai grezzi, mai il fornitore. L'`adj_close` del vendor si scarica ma
serve solo come confronto nei controlli: due fornitori normalizzano in momenti diversi, e
mescolare le due scale falserebbe ogni rendimento a cavallo della giunzione.

La seduta di oggi non viene mai salvata: a mercato aperto il close e' provvisorio, e
l'aggiornamento incrementale non riscrive le barre gia' salvate. La barra di oggi arriva
col download di domani.

## Il report di qualita'

Ogni download scrive `reports/data_quality_AAAA-MM-GG.md`, anche quando va tutto bene. In
testa: fornitore, modalita', calendario usato e soglie. Poi una riga per simbolo, e in
fondo il dettaglio dei problemi trovati.

Le sedute attese arrivano dal calendario Alpaca, lo stesso del live. Senza chiavi Alpaca o
senza rete si ripiega sull'unione delle date dei simboli scaricati, e il report lo dichiara:
in quel caso un giorno mancante in tutti i simboli non si vede.

Due severita'. `WARNING` scrive il Parquet e segnala: giorni mancanti o in piu' rispetto al
calendario, open o close appena fuori da `[low, high]`, volume zero, `adj_close` del vendor
che si scosta dal calcolo in casa fra lo 0,1% e il 5%. `BLOCKING` non scrive niente: date
duplicate o fuori ordine, prezzi o volumi mancanti, nulli o negativi, `high` sotto `low`,
un close che salta oltre il 25% senza uno split a spiegarlo, `adj_close` del vendor lontano
oltre il 5%. Le soglie stanno in `DataQualityConfig` in `quant/config.py`.

Con `--update` i finding riguardano solo le barre nuove: le vecchie fanno da contesto, per
esempio per misurare il salto fra l'ultima barra salvata e la prima nuova.

## Cosa fare con un BLOCKING

Il Parquet di quel simbolo non e' stato toccato: sul disco c'e' ancora la versione
precedente, e il manifest e' coerente con lei. Gli altri simboli sono stati scritti
normalmente. Non c'e' fretta e non c'e' niente da riparare a mano.

1. Leggi il dettaglio nel report: codice, data, valori.
2. Guarda la barra alla fonte, sul sito del fornitore o con l'altro fornitore:
   `uv run python scripts/download.py --check --source yfinance SIMBOLO`.
3. Se il dato del fornitore e' sbagliato, aspetta: le correzioni EOD arrivano di solito
   entro un giorno. Se il fornitore non corregge, scarica quel simbolo dall'altro
   fornitore con un download completo.
4. Se invece il dato e' giusto ed e' il mercato ad aver fatto quel movimento, alza la
   soglia in `DataQualityConfig` con un commento che dica quale evento l'ha richiesto.
   Un margine c'e' ma non e' grande: il +22,8% di EEM del 13 ottobre 2008 e' il record
   storico dell'universo attuale, contro una soglia al 25%.

Non modificare mai un Parquet a mano: l'aggiornamento successivo se ne accorge, confronta
lo SHA-256 col manifest e si rifiuta di accodare.

## Manifest e provenienza

`data/manifest.json` tiene, per ogni simbolo, fornitore, data di download, intervallo,
numero di barre e SHA-256 del Parquet. I Parquet non sono versionati, il manifest si': e'
la sola traccia in git di quali dati esistevano quando. **Va committato dopo ogni
download**, altrimenti la storia dei risultati resta senza riferimento.

Il manifest e' anche la guardia dell'aggiornamento incrementale. `--update` si ferma se la
voce manca, se il fornitore registrato e' diverso da quello richiesto, o se il Parquet non
corrisponde piu' al suo hash. In tutti e tre i casi la risposta e' la stessa: un download
completo, che riscrive file e voce insieme.

Ogni risultato di `run_backtest` porta un blocco `provenance` con l'impronta del backtest:
hash dei Parquet letti davvero, commit, se il working tree era sporco, configurazione,
finestra e parametri della strategia. Lo stampano in coda i report in `reports/` e il
report settimanale, e `sensitivity.csv` ha una colonna con l'impronta di ogni riga. Due
backtest con la stessa impronta devono dare lo stesso risultato; se non lo fanno, il
problema e' nel codice, non nei dati. L'hash del manifest si riporta ma resta fuori
dall'impronta, perche' cambierebbe anche aggiornando un simbolo che quel backtest non usa.

L'impronta usa l'hash dell'intero file: un backtest che finisce nel 2018 cambia impronta a
ogni aggiornamento, anche se le barre che legge sono le stesse. Confronta gli hash dei
dati, non l'impronta, quando vuoi sapere se le barre lette sono cambiate.

## Deriva retroattiva

    uv run python scripts/download.py --check

Riscarica l'intervallo gia' presente e lo confronta barra per barra con quello salvato,
senza scrivere niente. Confronta solo i valori grezzi: `adj_close` e' derivato e cambia per
costruzione a ogni nuova cedola. Le differenze finiscono in
`reports/data_drift_AAAA-MM-GG.csv`, una riga per valore cambiato, con tre tipi: `changed`,
`removed` per una barra che il fornitore non ha piu', `added` per una barra comparsa nel
passato. Ogni riga porta lo SHA-256 del Parquet confrontato, lo stesso che sta nella
provenienza dei backtest: da li' si risale ai risultati calcolati su quelle barre.

Una differenza retroattiva su una barra gia' usata in un backtest e' un evento, non un
inconveniente da sistemare in silenzio. Nell'ordine: leggi il CSV e guarda l'ampiezza,
decidi se rifare i backtest che hanno usato quell'hash, e solo dopo aggiorna i dati con un
download completo, committando il manifest nuovo. Se cancelli la differenza prima di
averla capita, hai perso l'unica occasione di sapere che i risultati vecchi non sono piu'
riproducibili.

Con una `--source` diversa da quella registrata nel manifest, `--check` diventa un
confronto fra fornitori: le differenze dicono quanto i due divergono, non che qualcuno ha
riscritto il passato.
