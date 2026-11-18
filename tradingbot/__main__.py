"""Einstiegspunkt.

  python3 -m tradingbot run            Entscheidungslauf, nur Vorschau (Trockenlauf)
  python3 -m tradingbot run --execute  Entscheidungslauf + Paper-Orders bei Alpaca
  python3 -m tradingbot apply decisions.json [--execute]
                                       Entscheidungen von Claude übernehmen (Risiko-Regeln gelten)
  python3 -m tradingbot status         Konto, Positionen, offene Orders
  python3 -m tradingbot flatten [--execute]  alle Positionen schließen
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path

from . import agents, http, risk
from .config import ROOT, Secrets, load_settings
from .sources import FMP, Alpaca, Finnhub, SecEdgar

REPORTS = ROOT / "reports"
LOGS = ROOT / "logs"


def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


class Run:
    def __init__(self):
        self.sec = Secrets()
        http.set_redactor(self.sec.redact)
        if not (self.sec.has("ALPACA_API_KEY") and self.sec.has("ALPACA_API_SECRET")):
            sys.exit("ABBRUCH: ALPACA_API_KEY / ALPACA_API_SECRET fehlen in keys.env.txt")
        self.s = load_settings()
        self.alp = Alpaca(self.sec)
        self.fh = Finnhub(self.sec)
        self.fmp = FMP(self.sec)
        self.edgar = SecEdgar(self.sec)
        self.errors: list[str] = []
        self.sources_used: set[str] = {"Alpaca"}
        self.disabled: set[str] = set()  # Endpunkte ohne Zugriff (Premium) für den Rest des Laufs überspringen

    def safe(self, label: str, fn, default):
        if label in self.disabled:
            return default
        try:
            return fn()
        except Exception as e:  # Quelle fällt aus -> weiter ohne sie
            msg = self.sec.redact(str(e))
            if any(f"HTTP {c}" in msg for c in (401, 402, 403)):
                self.disabled.add(label)
                msg = "kein Zugriff mit dem Gratis-Plan, für diesen Lauf übersprungen"
            if not any(msg[:60] in x for x in self.errors):
                self.errors.append(f"{label}: {msg[:200]}")
            return default

    # ---------- Daten sammeln ----------
    def gather(self) -> dict:
        account = self.alp.account()
        positions = self.alp.positions() or []
        clock = self.alp.clock()
        st = risk.portfolio_state(account, positions, clock)

        symbols = list(dict.fromkeys(s.upper() for s in self.s.watchlist))
        if self.s.add_most_actives:
            symbols += [x for x in self.safe("Alpaca Most-Actives", lambda: self.alp.most_actives(self.s.add_most_actives), []) if x not in symbols]
        symbols += [p for p in st["open_positions"] if p not in symbols]
        symbols = symbols[: max(self.s.max_symbols, len(st["open_positions"]))]

        daily = self.safe("Alpaca Tageskurse", lambda: self.alp.bars(symbols, "1Day", 100), {})
        # Penny-Stocks und illiquide Titel raus (außer bereits gehaltene)
        def liquid(sym):
            bs = daily.get(sym, [])
            if not bs:
                return False
            avg_dollar_vol = sum(b["c"] * b["v"] for b in bs[-20:]) / len(bs[-20:])
            return bs[-1]["c"] >= self.s.min_price and avg_dollar_vol >= self.s.min_dollar_volume
        dropped = [x for x in symbols if x not in st["open_positions"] and not liquid(x)]
        symbols = [x for x in symbols if x not in dropped]
        if dropped:
            self.errors.append(f"Ausgefiltert (Kurs < ${self.s.min_price:.0f} oder zu wenig Umsatz): {', '.join(dropped)}")
        intra = self.safe("Alpaca 15-Min-Kurse", lambda: self.alp.bars(symbols, "15Min", 5), {})
        metrics = {s: agents.quant_metrics(daily.get(s, []), intra.get(s, [])) for s in symbols}
        alp_news = self.safe("Alpaca News", lambda: self.alp.news(symbols, 24), [])

        general_news, earnings = [], []
        if self.fh.enabled:
            self.sources_used.add("Finnhub")
            general_news = self.safe("Finnhub General News", self.fh.general_news, [])
            earnings = self.safe("Finnhub Earnings", lambda: self.fh.earnings_calendar(2), [])
        earnings_soon = {e["symbol"] for e in earnings if e.get("symbol") in symbols}
        macro = agents.macro_politics(general_news)
        if self.fmp.enabled:
            self.sources_used.add("FMP")
        if self.edgar.enabled:
            self.sources_used.add("SEC EDGAR")

        opinions, details = {}, {}
        for sym in symbols:
            news = [{"headline": n.get("headline"), "summary": n.get("summary"), "source": n.get("source"), "time": n.get("created_at")}
                    for n in alp_news if sym in (n.get("symbols") or [])]
            rec, txs, cong, ratios = [], [], [], {}
            if self.fh.enabled:
                fn = self.safe(f"Finnhub News", lambda: self.fh.company_news(sym, 2), [])
                news += [{"headline": n.get("headline"), "summary": n.get("summary"), "source": n.get("source"),
                          "time": dt.datetime.fromtimestamp(n.get("datetime", 0), dt.timezone.utc).isoformat()} for n in fn[:15]]
                rec = self.safe("Finnhub Empfehlungen", lambda: self.fh.recommendation(sym), [])
                ratios = self.safe("Finnhub Kennzahlen", lambda: self.fh.basic_metrics(sym), {})
                txs = self.safe("Finnhub Insider", lambda: self.fh.insider_transactions(sym, 30), [])
                cong = self.safe("Finnhub Kongress-Trades (Premium?)", lambda: self.fh.congressional_trading(sym), [])
            seen, uniq = set(), []
            for n in news:
                h = (n.get("headline") or "").strip().lower()
                if h and h not in seen:
                    seen.add(h); uniq.append(n)
            news = uniq
            profile = {}
            if self.fmp.enabled:
                profile = self.safe("FMP Profil", lambda: self.fmp.profile(sym), {})
                if not cong:
                    cong = self.safe("FMP Senats-Trades (Premium?)", lambda: self.fmp.senate_trades(sym), [])
                    cutoff = str(dt.date.today() - dt.timedelta(days=60))
                    cong = [t for t in cong if str(t.get("transactionDate", "")) >= cutoff]
            filings = self.safe("SEC EDGAR", lambda: self.edgar.recent_filings(sym, 14), []) if self.edgar.enabled else []

            ops = [
                agents.insider(sym, txs, filings),
                agents.analyst(sym, news, rec, ratios, profile, sym in earnings_soon),
                agents.politics(sym, profile.get("companyName", ""), cong, general_news, profile.get("sector", "")),
                agents.quant(sym, metrics[sym]),
            ]
            opinions[sym] = ops
            details[sym] = {
                "company": profile.get("companyName"), "sector": profile.get("sector"),
                "metrics": metrics[sym],
                "news": news[:6],
                "insider_tx": [{"name": t.get("name"), "code": t.get("transactionCode"), "change": t.get("change"),
                                "price": t.get("transactionPrice"), "date": t.get("transactionDate")} for t in txs[:8]],
                "sec_filings": filings[:6],
                "politician_trades": cong[:6],
                "earnings_soon": sym in earnings_soon,
            }
        return {"account": account, "state": st, "clock": clock, "symbols": symbols, "metrics": metrics,
                "opinions": opinions, "details": details, "macro": macro, "earnings_soon": earnings_soon}

    # ---------- Ausführen ----------
    def execute(self, decisions: list[dict], st: dict, execute: bool) -> list[dict]:
        results = []
        for d in decisions:
            if d.get("final") not in ("BUY", "CLOSE"):
                continue
            r = {"symbol": d["symbol"], "action": d["final"], "status": "Vorschau (Trockenlauf)"}
            if execute:
                if not st["market_open"]:
                    r["status"] = "nicht gesendet: Börse geschlossen"
                else:
                    try:
                        if d["final"] == "BUY":
                            o = self.alp.submit_order(d["order"])
                        else:
                            self.safe("Orders stornieren", lambda: self.alp.cancel_orders_for(d["symbol"]), None)
                            o = self.alp.close_position(d["symbol"])
                        r["status"] = f"gesendet (Order {o.get('id', '?')[:8]}, {o.get('status')})"
                        r["order_id"] = o.get("id")
                    except Exception as e:
                        r["status"] = f"FEHLER: {self.sec.redact(str(e))[:200]}"
            results.append(r)
        return results


# ---------- Bericht ----------
def fmt_report(mode: str, g: dict, decisions: list[dict], results: list[dict], run: Run, execute: bool) -> str:
    st = g["state"]
    t = now_local()
    L = [f"# Entscheidungslauf {t:%d.%m.%Y %H:%M} ({mode}, {'PAPER-ORDERS' if execute else 'Trockenlauf'})", ""]
    L += ["## Konto (Alpaca Paper)",
          f"- Eigenkapital: ${st['equity']:,.2f} · Cash: ${st['cash']:,.2f} · Kaufkraft: ${st['buying_power']:,.2f}",
          f"- Tages-P&L: {st['day_pnl_pct']:+.2f}% · Börse: {'offen' if st['market_open'] else 'geschlossen'}"
          + (f" · {st['minutes_to_close']:.0f} Min. bis Schluss" if st['market_open'] and st['minutes_to_close'] else
             f" · nächste Öffnung {g['clock'].get('next_open', '?')}"),
          f"- Offene Positionen: {', '.join(f'{s} ({float(p['unrealized_plpc'])*100:+.1f}%)' for s, p in st['open_positions'].items()) or 'keine'}",
          f"- Datenquellen: {', '.join(sorted(run.sources_used))}", ""]
    m = g["macro"]
    L += ["## Politik/Makro (marktweit)", f"- {m['count']} politische Schlagzeilen, Ton-Saldo {m['tone']:+d}"]
    L += [f"  - {h}" for h in m["headlines"][:4]]
    L += ["", "## Entscheidungen des Risikomanagers", ""]
    acts = [d for d in decisions if d.get("final") in ("BUY", "CLOSE")]
    if not acts:
        L.append("Keine Trades. Begründung je Kandidat unten.")
    for d in acts:
        L.append(f"- **{d['final']} {d['symbol']}**: {d.get('why', '')}" + (f" · {d['sizing']}" if d.get("sizing") else ""))
    for r in results:
        L.append(f"  - Ausführung {r['symbol']}: {r['status']}")
    vetoed = [d for d in decisions if d.get("vetoes") and d.get("final") == "HOLD"]
    if vetoed:
        L += ["", "**Veto des Risikomanagers:**"]
        L += [f"- {d['symbol']} (Score {d.get('score', 0):+.1f}): {'; '.join(d['vetoes'])}" for d in vetoed]
    L += ["", "## Bewertungen je Aktie", "", "| Aktie | Insider | Analyst | Politik | Quant | Summe | Kurs | Entscheidung |",
          "|---|---|---|---|---|---|---|---|"]
    order = sorted(g["symbols"], key=lambda s: -sum(o.score for o in g["opinions"][s]))
    final = {d["symbol"]: d.get("final", "HOLD") for d in decisions}
    for s in order:
        ops = {o.role: o.score for o in g["opinions"][s]}
        L.append(f"| {s} | {ops.get('Insider-Beobachter', 0):+.1f} | {ops.get('Analyst', 0):+.1f} | {ops.get('Politik-Analyst', 0):+.1f} | "
                 f"{ops.get('Quant', 0):+.1f} | **{sum(ops.values()):+.1f}** | {g['metrics'][s].get('price') or '-'} | {final.get(s, 'HOLD')} |")
    L += ["", "## Begründungen (Top 8)"]
    for s in order[:8]:
        L.append(f"### {s}" + (f" – {g['details'][s]['company']}" if g['details'][s].get('company') else ""))
        for o in g["opinions"][s]:
            L.append(f"- *{o.role}* ({o.score:+.1f}): " + "; ".join(o.reasons))
    if run.errors:
        L += ["", "## Hinweise zu Datenquellen"] + [f"- {e}" for e in run.errors[:12]]
    L += ["", "_Nur Paper-Trading. Regelbasierte Simulation, keine Anlageberatung._"]
    return "\n".join(L)


def write_outputs(mode: str, g: dict, decisions: list, results: list, run: Run, execute: bool) -> Path:
    REPORTS.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    t = now_local()
    stem = f"{t:%Y-%m-%d_%H%M}_{mode}"
    md = REPORTS / f"{stem}.md"
    md.write_text(fmt_report(mode, g, decisions, results, run, execute), encoding="utf-8")
    briefing = {
        "time": t.isoformat(), "mode": mode, "executed": execute,
        "state": {k: v for k, v in g["state"].items() if k != "open_positions"},
        "positions": {s: {k: p.get(k) for k in ("qty", "avg_entry_price", "current_price", "unrealized_plpc")} for s, p in g["state"]["open_positions"].items()},
        "macro": g["macro"],
        "symbols": {s: {**g["details"][s], "opinions": [o.as_dict() for o in g["opinions"][s]]} for s in g["symbols"]},
        "decisions": decisions, "results": results, "source_errors": run.errors,
    }
    (REPORTS / f"{stem}.json").write_text(json.dumps(briefing, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
    (REPORTS / "latest.md").write_text(md.read_text(encoding="utf-8"), encoding="utf-8")
    (REPORTS / "latest.json").write_text((REPORTS / f"{stem}.json").read_text(encoding="utf-8"), encoding="utf-8")
    if results:
        new = not (LOGS / "trades.csv").exists()
        with open(LOGS / "trades.csv", "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["zeit", "symbol", "aktion", "status", "order_id", "modus"])
            for r in results:
                w.writerow([t.isoformat(timespec="seconds"), r["symbol"], r["action"], r["status"], r.get("order_id", ""), "execute" if execute else "dry"])
    with open(LOGS / "runs.log", "a", encoding="utf-8") as f:
        acts = ", ".join(f"{d['final']} {d['symbol']}" for d in decisions if d.get("final") in ("BUY", "CLOSE")) or "keine Trades"
        f.write(f"{t.isoformat(timespec='seconds')} {mode} {'EXECUTE' if execute else 'DRY'} equity={g['state']['equity']:.2f} -> {acts}\n")
    return md


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tradingbot", description="Paper-Trading-Agentengruppe (nur Alpaca Paper)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="Entscheidungslauf")
    p_run.add_argument("--execute", action="store_true", help="Paper-Orders wirklich senden")
    p_apply = sub.add_parser("apply", help="Entscheidungen aus JSON (z. B. von Claude) anwenden")
    p_apply.add_argument("file")
    p_apply.add_argument("--execute", action="store_true")
    sub.add_parser("status", help="Konto und Positionen")
    p_fl = sub.add_parser("flatten", help="alle Positionen schließen")
    p_fl.add_argument("--execute", action="store_true")
    a = ap.parse_args(argv)

    run = Run()
    if a.cmd == "status":
        acc, pos, clk = run.alp.account(), run.alp.positions(), run.alp.clock()
        st = risk.portfolio_state(acc, pos, clk)
        print(f"Paper-Konto: Eigenkapital ${st['equity']:,.2f}, Cash ${st['cash']:,.2f}, Tages-P&L {st['day_pnl_pct']:+.2f}%, "
              f"Börse {'offen' if st['market_open'] else 'geschlossen'}")
        for s, p in st["open_positions"].items():
            print(f"  {s}: {p['qty']} Stk. @ {p['avg_entry_price']} -> {p['current_price']} ({float(p['unrealized_plpc'])*100:+.2f}%)")
        print(f"  offene Orders: {len(run.alp.open_orders() or [])}")
        return

    if a.cmd == "flatten":
        acc, pos, clk = run.alp.account(), run.alp.positions(), run.alp.clock()
        st = risk.portfolio_state(acc, pos, clk)
        dec = [{"symbol": s, "action": "CLOSE", "final": "CLOSE", "why": "manuell glattstellen", "vetoes": []} for s in st["open_positions"]]
        res = run.execute(dec, st, a.execute)
        for r in res:
            print(f"{r['symbol']}: {r['status']}")
        if not res:
            print("Keine offenen Positionen.")
        return

    g = run.gather()
    if a.cmd == "run":
        decisions = risk.decide(g["symbols"], g["opinions"], g["metrics"], g["state"], run.s, g["macro"], g["earnings_soon"])
        mode = "regelbasiert"
    else:
        raw = json.loads(Path(a.file).read_text(encoding="utf-8"))
        items = raw.get("decisions", raw) if isinstance(raw, dict) else raw
        props = []
        for it in items:
            sym = str(it["symbol"]).upper()
            act = str(it.get("action", "HOLD")).upper()
            if sym not in g["metrics"]:
                g["metrics"][sym] = agents.quant_metrics(
                    run.safe("Alpaca Kurse", lambda: run.alp.bars([sym], "1Day", 100), {}).get(sym, []),
                    run.safe("Alpaca Kurse", lambda: run.alp.bars([sym], "15Min", 5), {}).get(sym, []))
            props.append({"symbol": sym, "action": act, "score": float(it.get("conviction", 3)),
                          "size_factor": float(it.get("size", 1.0)), "why": f"Claude: {it.get('reason', '')}", "from_claude": True})
        decisions = risk.enforce(props, g["metrics"], g["state"], run.s, g["macro"], g["earnings_soon"])
        mode = "claude"
    results = run.execute(decisions, g["state"], a.execute)
    md = write_outputs(mode, g, decisions, results, run, a.execute)
    acts = [f"{d['final']} {d['symbol']}" for d in decisions if d.get("final") in ("BUY", "CLOSE")]
    print(f"Bericht: {md}")
    print(f"Entscheidungen: {', '.join(acts) or 'keine Trades'}")
    for r in results:
        print(f"  {r['symbol']}: {r['status']}")
    if run.errors:
        print(f"Hinweise zu Datenquellen: {len(run.errors)} (siehe Bericht)")


if __name__ == "__main__":
    main()
