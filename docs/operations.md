# Operations: leggere il confronto fra live e backtest

Il paper trading produce numeri veri. Lo shadow backtest rigioca la stessa strategia sui
prezzi storici definitivi. La differenza fra i due e' l'unica misura onesta di quanto
costa passare dalla simulazione al mercato.

## Comandi

    uv run python scripts/download.py --update      # aggiorna i Parquet, solo le barre nuove
    uv run python scripts/weekly_report.py          # report della settimana corrente
    uv run python scripts/weekly_report.py --giorno 2026-09-11 --avvio 2026-09-01
    uv run python scripts/ui.py                     # le stesse misure nel browser

Tutti gli script di analisi sono in sola lettura: aprono lo StateStore, i Parquet e i log,
e scrivono soltanto dentro `reports/`. Non inviano ordini e non modificano lo stato live.

Fra un report e l'altro, la dashboard mostra lo stesso confronto aggiornato ogni minuto: la
pagina Performance il tracking error dall'avvio del live e i costi dei due lati, la pagina
Ordini lo scarto di ogni eseguito dal fill teorico dello shadow. La guida e' in [ui.md](ui.md).

Le decisioni del gestore del rischio e le anomalie arrivano dal log JSON che lo scheduler
scrive in `logs/live.jsonl`. Se si sposta il file con `QUANT_LOG_FILE`, va passato anche
al report con `--log`, altrimenti quelle sezioni restano vuote.

## Cosa guardare, nell'ordine

1. **Il blocco ATTENZIONE**, se c'e'. Sta in cima e dice cosa e dove. Se non c'e', la
   settimana e' andata come previsto e bastano trenta secondi di lettura.
2. **Le tre righe di sintesi**: tracking error cumulato, slippage reale contro simulato,
   conteggio degli eseguiti e dei giorni saltati.
3. **La tabella degli eseguiti**: una riga per operazione, con lo scarto in punti base fra
   il prezzo pagato davvero e quello che il backtest aveva ipotizzato.
4. **Le anomalie**: disallineamenti, eccezioni, giornate in cui il runner non ha girato.
   Una giornata saltata non e' un dettaglio, e' un segnale mancato.

## Tracking error

E' la distanza fra l'equity live e quella dello shadow. Il report ne da' due forme.

| Misura | Cosa dice |
|---|---|
| Giornaliero | Deviazione standard della differenza fra i rendimenti giornalieri |
| Cumulato | Distanza fra i due rendimenti totali dall'avvio del live |

Il cumulato oltre 50 punti base fa scattare l'attenzione. La soglia si cambia con
`--soglia`, ma prima di alzarla conviene capire perche' scatta. Le cause tipiche, in
ordine di frequenza: prezzi di eseguito peggiori del previsto, ordini tagliati dal gestore
del rischio che nel backtest passavano interi, giornate in cui il runner non ha girato,
eseguiti mancanti da un lato solo.

Un tracking error che cresce sempre nella stessa direzione e' un costo sistematico. Uno
che oscilla intorno allo zero e' rumore di esecuzione ed e' fisiologico.

## Slippage

Entrambi i lati sono misurati contro lo stesso riferimento, l'apertura ufficiale della
giornata presa dal Parquet, con il segno scelto in modo che un valore positivo significhi
sempre esecuzione sfavorevole. Cosi' i due numeri sono confrontabili.

Se lo slippage reale medio supera il doppio di quello simulato, il report apre con
l'attenzione. Vuol dire che il backtest sta promettendo rendimenti che il mercato non
concede. Guardare la tabella per simbolo: spesso il problema e' concentrato sui titoli
meno liquidi, non su tutto il portafoglio.

## Quando aggiornare i parametri di costo

Si aggiornano quando lo slippage reale medio si discosta stabilmente da quello simulato,
misurato su almeno un trimestre di operativita' e su non meno di trenta eseguiti. Il nuovo
valore da usare e' la mediana dello slippage reale per simbolo, non la media: qualche
eseguito anomalo non deve spostare l'ipotesi di costo di tutto il sistema.

Dopo l'aggiornamento vanno rifatti i backtest della Fase 2 con i costi nuovi. Se la
strategia non regge i costi veri, la risposta e' smettere di operarla, non ritoccarla.

## La regola che non si tocca

**Si aggiornano solo i costi, mai i parametri della strategia sulla base dei risultati
live.**

Il periodo live e' un campione minuscolo e, soprattutto, e' l'unico dato davvero fuori
campione che esiste. Ritoccare lookback, numero di posizioni o filtro di trend perche'
"in live ha funzionato meglio cosi'" lo trasforma in un altro periodo di ottimizzazione, e
a quel punto non resta piu' niente con cui verificare la strategia. Il Deflated Sharpe
della Fase 2 conta le configurazioni provate: ogni ritocco fatto guardando il live aggiunge
una prova che nessuno sta contando.

I costi sono diversi perche' non sono un parametro della strategia: sono una misura del
mercato, e misurarli meglio rende il backtest piu' onesto, non piu' compiacente.

Se il live si discosta e i costi sono gia' allineati, le uscite legittime sono due:
lasciare tutto com'e' e continuare a misurare, oppure fermare la strategia. Non c'e' una
terza via che passi per i parametri.
