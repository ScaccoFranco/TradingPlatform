"""Dashboard FastAPI: solo rotte GET, dati in sola lettura, nessun JavaScript.

Ogni pagina chiama lo strato dati di `quant.webui.read` e rende un template Jinja; qui
non si calcola niente. Niente sessioni, cookie o documentazione interattiva, che
scaricherebbe script da una CDN. Si risponde solo a richieste indirizzate al loopback.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from starlette.middleware.trustedhost import TrustedHostMiddleware

from quant.config import PERCORSO_DATI
from quant.logreader import PERCORSO_LOG
from quant.risk import PERCORSO_KILL
from quant.state import PERCORSO_DB
from quant.webui.avvisi import Avviso, avvisi
from quant.webui.charts import grafico_drawdown, grafico_equity, grafico_esposizione
from quant.webui.formato import (
    bps,
    bps_opz,
    classe_stato,
    descrivi_evento,
    descrivi_motivo,
    dimensione,
    momento_borsa,
    tabella_metriche,
    timestamp_testo,
    valuta,
)
from quant.webui.read import (
    GIORNI_ORDINI,
    MASSIMO_TESTO,
    SCELTE_GIORNI,
    TIPI_IMMAGINE,
    TIPO_ALTRO,
    TIPO_IMMAGINE,
    CalcoloOmbra,
    OmbraMemorizzata,
    Ritardo,
    Sorgenti,
    StatoLive,
    anomalie_recenti,
    elenco_report,
    filtro_ordini,
    giorni_finestra,
    immagine_collegata,
    leggi_testo,
    oggi_in_borsa,
    ombra_del_live,
    ordini_e_eseguiti,
    ordini_recenti,
    performance,
    rischio,
    risolvi_report,
    ritardo_esecuzione,
    stato_live,
    tipo_report,
)
from quant.weekly import BENCHMARK, REPORT, ConfrontoLive

CARTELLA = Path(__file__).parent
HOST_AMMESSI = ("127.0.0.1", "localhost")
AGGIORNAMENTO_SECONDI = 60
ORDINI_IN_HOME = 10
PAGINE = (
    ("/", "Stato"),
    ("/performance", "Performance"),
    ("/ordini", "Ordini"),
    ("/rischio", "Rischio"),
    ("/report", "Report"),
)
INTESTAZIONI = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def create_app(
    db: str | Path = PERCORSO_DB,
    data_dir: str | Path = PERCORSO_DATI,
    reports_dir: str | Path = REPORT,
    log: str | Path = PERCORSO_LOG,
    kill: str | Path = PERCORSO_KILL,
    oggi: Callable[[], date] = oggi_in_borsa,
    ombra: CalcoloOmbra = ombra_del_live,
    host_ammessi: Sequence[str] = HOST_AMMESSI,
) -> FastAPI:
    """Costruisce l'app sui percorsi indicati.

    `oggi` e `ombra` si iniettano nei test: il primo fissa il calendario, il secondo
    sostituisce la rigiocata dello shadow sull'universo vero. Il controllo dell'header
    Host, limitato a `host_ammessi`, tiene fuori le pagine web che con un DNS rebinding
    farebbero leggere la dashboard al browser di chi la sta guardando.
    """
    sorgenti = Sorgenti(Path(db), Path(data_dir), Path(reports_dir), Path(log), Path(kill))
    templates = _templates()
    memoria = OmbraMemorizzata(ombra, sorgenti.data_dir)
    app = FastAPI(title="quant, dashboard di sola lettura", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(host_ammessi))
    app.mount("/static", StaticFiles(directory=CARTELLA / "static"), name="static")

    @app.middleware("http")
    async def intestazioni(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        risposta = await call_next(request)
        risposta.headers.update(INTESTAZIONI)
        return risposta

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Il processo risponde: non apre database, log ne' Parquet."""
        return {"stato": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        """Stato live con gli avvisi in cima."""
        stato, ritardo, elenco = _quadro(sorgenti, oggi())
        contesto = {
            **_comune("/", elenco),
            "stato": stato,
            "ritardo": ritardo,
            "ordini": ordini_recenti(sorgenti, ORDINI_IN_HOME),
            "sorgenti": sorgenti,
        }
        return templates.TemplateResponse(request, "stato.html", contesto)

    @app.get("/performance", response_class=HTMLResponse)
    def pagina_performance(request: Request) -> HTMLResponse:
        """Live contro shadow contro SPY: grafici, metriche e tracking error."""
        giorno = oggi()
        _, _, elenco = _quadro(sorgenti, giorno)
        risultato, nota_ombra = memoria(giorno)
        dati = performance(sorgenti, risultato, nota_ombra)
        contesto = {**_comune("/performance", elenco), "dati": dati, **_vista_performance(dati.confronto)}
        return templates.TemplateResponse(request, "performance.html", contesto)

    @app.get("/ordini", response_class=HTMLResponse)
    def pagina_ordini(
        request: Request, dal: str | None = None, al: str | None = None, simbolo: str | None = None
    ) -> HTMLResponse:
        """Ordini ed eseguiti filtrati per giornate e simbolo, con lo scarto dallo shadow."""
        giorno = oggi()
        _, _, elenco = _quadro(sorgenti, giorno)
        filtro = filtro_ordini(dal, al, simbolo, giorno)
        risultato, nota_ombra = memoria(giorno)
        contesto = {
            **_comune("/ordini", elenco),
            "filtro": filtro,
            "dati": ordini_e_eseguiti(sorgenti, filtro, risultato),
            "nota_shadow": nota_ombra,
            "giorni_predefiniti": GIORNI_ORDINI,
        }
        return templates.TemplateResponse(request, "ordini.html", contesto)

    @app.get("/rischio", response_class=HTMLResponse)
    def pagina_rischio(request: Request, giorni: str | None = None) -> HTMLResponse:
        """Decisioni del RiskManager raggruppate per motivo e anomalie della finestra scelta."""
        giorno = oggi()
        _, _, elenco = _quadro(sorgenti, giorno)
        finestra = giorni_finestra(giorni)
        contesto = {
            **_comune("/rischio", elenco),
            "dati": rischio(sorgenti, finestra, giorno),
            "giorni": finestra,
            "scelte": SCELTE_GIORNI,
        }
        return templates.TemplateResponse(request, "rischio.html", contesto)

    @app.get("/report", response_class=HTMLResponse)
    def pagina_report(request: Request) -> HTMLResponse:
        """File prodotti dal report settimanale e dagli script di ricerca."""
        _, _, elenco = _quadro(sorgenti, oggi())
        contesto = {
            **_comune("/report", elenco),
            "report": elenco_report(sorgenti),
            "cartella": sorgenti.reports_dir,
        }
        return templates.TemplateResponse(request, "report.html", contesto)

    @app.get("/report/{nome:path}")
    def file_report(request: Request, nome: str) -> Response:
        """Un report: il testo dentro <pre>, le immagini come file statici, tutto il resto 404."""
        percorso = risolvi_report(sorgenti.reports_dir, nome)
        if percorso is None or tipo_report(percorso) == TIPO_ALTRO:
            raise HTTPException(status_code=404, detail="report non trovato")
        if tipo_report(percorso) == TIPO_IMMAGINE:
            return FileResponse(percorso, media_type=TIPI_IMMAGINE[percorso.suffix.lower()])
        try:
            testo, troncato = leggi_testo(percorso)
        except OSError as errore:
            raise HTTPException(status_code=404, detail="report non leggibile") from errore
        _, _, elenco = _quadro(sorgenti, oggi())
        contesto = {
            **_comune("/report", elenco),
            "nome": nome,
            "testo": testo,
            "troncato": troncato,
            "massimo": MASSIMO_TESTO,
            "immagine": immagine_collegata(sorgenti.reports_dir, percorso),
        }
        return templates.TemplateResponse(request, "report_file.html", contesto)

    return app


def _quadro(sorgenti: Sorgenti, giorno: date) -> tuple[StatoLive, Ritardo | None, tuple[Avviso, ...]]:
    """Stato, ritardo del runner e avvisi: quello che ogni pagina mostra in cima."""
    stato = stato_live(sorgenti)
    ritardo = ritardo_esecuzione(sorgenti, stato, giorno)
    return stato, ritardo, avvisi(stato, anomalie_recenti(sorgenti, oggi=giorno), ritardo, sorgenti)


def _vista_performance(confronto: ConfrontoLive | None) -> dict[str, Any]:
    """Grafici SVG e righe della tabella; vuoti se non c'e' un periodo da confrontare."""
    if confronto is None or confronto.inizio is None:
        return {"grafici": {}, "colonne": (), "metriche": ()}
    serie = {"live": confronto.live, "shadow": confronto.shadow, BENCHMARK: confronto.benchmark}
    return {
        "grafici": {
            "equity": grafico_equity(serie),
            "drawdown": grafico_drawdown(confronto.drawdown),
            "esposizione": grafico_esposizione(confronto.esposizione),
        },
        "colonne": tuple(confronto.metriche),
        "metriche": tabella_metriche(confronto.metriche, senza_esecuzione=(BENCHMARK,)),
    }


def _comune(percorso: str, elenco: tuple[Avviso, ...]) -> dict[str, Any]:
    """Contesto che serve a `base.html`."""
    return {
        "pagine": PAGINE,
        "pagina": percorso,
        "avvisi": elenco,
        "aggiornamento": AGGIORNAMENTO_SECONDI,
        "generata": momento_borsa(datetime.now(UTC)),
    }


def _templates() -> Jinja2Templates:
    """Template con autoescape e variabili indefinite trattate come errori."""
    ambiente = Environment(
        loader=FileSystemLoader(CARTELLA / "templates"), autoescape=True, undefined=StrictUndefined
    )
    ambiente.filters.update(
        valuta=valuta,
        bps=bps,
        bps_opz=bps_opz,
        classe_stato=classe_stato,
        descrivi_evento=descrivi_evento,
        descrivi_motivo=descrivi_motivo,
        dimensione=dimensione,
        momento_borsa=momento_borsa,
        timestamp_testo=timestamp_testo,
    )
    return Jinja2Templates(env=ambiente)
