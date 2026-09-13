"""Le due contabilita' del total return a confronto, sul periodo in-sample.

Stesse strategie e stessi costi di `insample`, cambia solo `BacktestConfig.dividends_as_cash`:
cedole in cassa sui prezzi grezzi contro total return nel prezzo rettificato. Uno scarto
grande vorrebbe dire che una delle due contabilita' perde o inventa cedole.

La tolleranza e' 0.25% sull'equity finale di tredici anni, circa 2 bps l'anno. Le due
contabilita' non possono coincidere: con le cedole in cassa il contante resta fermo fino al
ribilanciamento successivo, fino a un mese, mentre `adj_close` lo reinveste alla data ex.
Lo scarto atteso e' rendimento da cedola per rendimento del mese perso, pochi bps l'anno e
di segno negativo quando il mercato sale; si aggiungono l'arrotondamento alle azioni intere e
costi di ribilanciamento di poco diversi. Perdere una sola cedola trimestrale di SPY vale gia'
circa lo 0.5%: la soglia resta ben sotto quello che si vuole scoprire.
"""

from __future__ import annotations

from quant.analysis import to_series
from quant.provenance import provenance_markdown
from quant.research.common import CONFIG, REPORT, confronto, quiet_logging
from quant.research.insample import FINE, INIZIO, INIZIO_RISCALDATO
from quant.validation import BacktestResult

TOLLERANZA = 0.0025


def cagr(risultato: BacktestResult) -> float:
    """Rendimento annuo composto dell'equity curve."""
    serie = to_series(risultato["equity_curve"])
    anni = (serie.index[-1] - serie.index[0]).days / 365.25
    return float((serie.iloc[-1] / serie.iloc[0]) ** (1.0 / anni) - 1.0)


def scarto_finale(cassa: BacktestResult, rettificato: BacktestResult) -> float:
    """Scarto relativo dell'equity finale con le cedole in cassa rispetto ai prezzi rettificati."""
    return float(cassa["equity_curve"][-1][1] / rettificato["equity_curve"][-1][1] - 1.0)


def tabella(in_cassa: dict[str, BacktestResult], rettificati: dict[str, BacktestResult]) -> str:
    """Markdown con equity finale, scarto e cedole incassate per strategia."""
    righe = [
        "| Strategia | Equity cedole in cassa | Equity prezzi rettificati | Scarto | Scarto CAGR "
        "| Cedole incassate |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for nome, cassa in in_cassa.items():
        rettificato = rettificati[nome]
        finale_cassa = cassa["equity_curve"][-1][1]
        finale_rettificato = rettificato["equity_curve"][-1][1]
        scarto = scarto_finale(cassa, rettificato)
        scarto_cagr = (cagr(cassa) - cagr(rettificato)) * 10_000
        cedole = cassa["portfolio"].dividends_received
        righe.append(
            f"| {nome} | {finale_cassa:,.0f} | {finale_rettificato:,.0f} | {scarto:+.3%} "
            f"| {scarto_cagr:+.1f} bps | {cedole:,.0f} |"
        )
    return "\n".join(righe)


def main() -> None:
    """Esegue i due confronti e scrive `reports/dividend_accounting.md`."""
    quiet_logging()
    in_cassa = confronto(INIZIO_RISCALDATO, FINE, INIZIO, CONFIG.with_overrides(dividends_as_cash=True))
    rettificati = confronto(INIZIO_RISCALDATO, FINE, INIZIO, CONFIG.with_overrides(dividends_as_cash=False))
    peggiore = max(abs(scarto_finale(in_cassa[n], rettificati[n])) for n in in_cassa)
    esito = "entro" if peggiore <= TOLLERANZA else "FUORI"
    testo = (
        f"# Contabilita' delle cedole, {INIZIO_RISCALDATO} -> {FINE}\n\n{tabella(in_cassa, rettificati)}\n\n"
        f"Scarto massimo {peggiore:.3%}, {esito} la tolleranza del {TOLLERANZA:.2%}.\n"
    )
    print(testo)
    backtest = {f"{n} in cassa": r for n, r in in_cassa.items()} | {
        f"{n} rettificati": r for n, r in rettificati.items()
    }
    REPORT.mkdir(parents=True, exist_ok=True)
    (REPORT / "dividend_accounting.md").write_text(
        f"{testo}\n{provenance_markdown(backtest)}", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
