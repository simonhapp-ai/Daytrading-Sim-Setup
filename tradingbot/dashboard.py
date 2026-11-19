"""Exportiert Daten für die beiden Claude-Artefakte (Portfolio-Dashboard und Agenten-Chatraum).

  python3 -m tradingbot.dashboard

Schreibt nach `dashboard/`:
  portfolio.json  Konto, Positionen, Equity-Verlauf (aus Alpaca), Orders der letzten Wochen
  chat.json       Gesprächsverlauf der fünf Rollen je Entscheidungslauf (aus reports/*.json)

Es landen KEINE Keys, Kontonummern oder IDs des Kontos in den Dateien: nur Zahlen,
Symbole und die Begründungen der Rollen. Danach kopiert die geplante Aufgabe den Ordner
in die Projektdateien, wo die Artefakte ihn lesen.

Optional kann eine Entscheidungsdatei von Claude (decisions/*.json) ein Feld
"discussion" enthalten, das im Chatraum zwischen den Rollen-Meinungen und dem
Urteil des Risikomanagers erscheint:
  {"decisions": [...], "discussion": [
     {"role": "Quant", "symbol": "AMZN", "text": "Trend intakt, aber Momentum schwach."},
     {"role": "Analyst", "symbol": "AMZN", "text": "Die AWS-News wiegen das auf."}
  ]}
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import http
from .config import ROOT, Secrets
from .sources import Alpaca

OUT = ROOT / "dashboard"
REPORTS = ROOT / "reports"
DECISIONS = ROOT / "decisions"
MAX_RUNS = 60          # so viele Läufe zeigt der Chatraum
TOP_PER_RUN = 4        # zusätzlich zu Trades/Vetos die besten Kandidaten je Lauf
ROLES = ["Insider-Beobachter", "Analyst", "Politik-Analyst", "Quant", "Risikomanager"]


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


# ---------------- Portfolio ----------------
def _history(alp: Alpaca, period: str, timeframe: str, extended: bool = False) -> list[dict]:
    p = {"period": period, "timeframe": timeframe}
    if extended:
        p["intraday_reporting"] = "extended_hours"
    r = http.get(f"{alp.base}/account/portfolio/history", headers=alp.h, params=p) or {}
    pts = []
    for t, e, pl in zip(r.get("timestamp") or [], r.get("equity") or [], r.get("profit_loss") or []):
        if e is None:
            continue
        pts.append({"t": _iso(t), "equity": round(_f(e), 2), "pl": round(_f(pl), 2)})
    return pts


def portfolio(sec: Secrets) -> dict:
    alp = Alpaca(sec)
    errors = []

    def safe(label, fn, default):
        try:
            return fn()
        except Exception as e:
            errors.append(f"{label}: {sec.redact(str(e))[:160]}")
            return default

    acc = alp.account() or {}
    clock = safe("Börsenuhr", alp.clock, {}) or {}
    pos = safe("Positionen", alp.positions, []) or []
    orders = safe("Orders", lambda: http.get(f"{alp.base}/orders", headers=alp.h, params={
        "status": "all", "limit": 200, "nested": "true", "direction": "desc",
        "after": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")}), []) or []

    equity, last = _f(acc.get("equity")), _f(acc.get("last_equity"))
    positions = []
    for p in pos:
        positions.append({
            "symbol": p.get("symbol"), "qty": _f(p.get("qty")), "side": p.get("side"),
            "avg_entry": _f(p.get("avg_entry_price")), "price": _f(p.get("current_price")),
            "market_value": _f(p.get("market_value")), "cost_basis": _f(p.get("cost_basis")),
            "unrealized_pl": _f(p.get("unrealized_pl")), "unrealized_plpc": _f(p.get("unrealized_plpc")) * 100,
            "intraday_plpc": _f(p.get("unrealized_intraday_plpc")) * 100,
        })

    def order_row(o):
        legs = o.get("legs") or []
        stop = next((l.get("stop_price") for l in legs if l.get("stop_price")), None)
        take = next((l.get("limit_price") for l in legs if l.get("type") == "limit"), None)
        return {
            "time": o.get("submitted_at") or o.get("created_at"), "filled_at": o.get("filled_at"),
            "symbol": o.get("symbol"), "side": o.get("side"), "type": o.get("type"),
            "order_class": o.get("order_class") or "simple", "qty": _f(o.get("qty")),
            "filled_qty": _f(o.get("filled_qty")), "filled_avg_price": _f(o.get("filled_avg_price"), None),
            "limit_price": _f(o.get("limit_price"), None), "stop": _f(stop, None), "take_profit": _f(take, None),
            "status": o.get("status"), "extended_hours": bool(o.get("extended_hours")),
            "legs": [{"type": l.get("type"), "side": l.get("side"), "status": l.get("status"),
                      "filled_avg_price": _f(l.get("filled_avg_price"), None), "filled_at": l.get("filled_at")} for l in legs],
        }

    return {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "account": {
            "equity": equity, "last_equity": last, "cash": _f(acc.get("cash")),
            "buying_power": _f(acc.get("buying_power")), "long_market_value": _f(acc.get("long_market_value")),
            "day_pl": equity - last, "day_pl_pct": (equity / last - 1) * 100 if last else 0.0,
            "status": acc.get("status"), "paper": "paper-api" in alp.base,
        },
        "clock": {"is_open": clock.get("is_open"), "next_open": clock.get("next_open"), "next_close": clock.get("next_close")},
        "positions": positions,
        "history": {
            "intraday": safe("Verlauf heute", lambda: _history(alp, "1D", "5Min", True), []),
            "week": safe("Verlauf Woche", lambda: _history(alp, "1W", "1H", True), []),
            "month": safe("Verlauf Monat", lambda: _history(alp, "1M", "1D"), []),
            "all": safe("Verlauf gesamt", lambda: _history(alp, "1A", "1D"), []),
        },
        "orders": [order_row(o) for o in orders],
        "errors": errors,
    }


# ---------------- Chatraum ----------------
def _decision_file_for(run_time: dt.datetime, used: set) -> dict | None:
    """Sucht die Claude-Entscheidungsdatei, die kurz vor dem Lauf geschrieben wurde."""
    if not DECISIONS.exists():
        return None
    best = None
    for f in DECISIONS.glob("*.json"):
        if f.name == "beispiel.json" or f in used:
            continue
        m = dt.datetime.fromtimestamp(f.stat().st_mtime).astimezone()
        delta = (run_time - m).total_seconds()
        if -120 <= delta <= 1800 and (best is None or delta < best[0]):
            best = (delta, f)
    if not best:
        return None
    used.add(best[1])
    try:
        return json.loads(best[1].read_text(encoding="utf-8"))
    except Exception:
        return None


def _opinion_text(op: dict) -> str:
    reasons = [r for r in op.get("reasons") or [] if r]
    return "; ".join(reasons) if reasons else "keine Daten"


def chat() -> dict:
    files = sorted(p for p in REPORTS.glob("20*.json"))[-MAX_RUNS:] if REPORTS.exists() else []
    runs, used = [], set()
    for f in files:
        try:
            b = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        t = dt.datetime.fromisoformat(b["time"])
        st = b.get("state", {})
        claude_file = _decision_file_for(t, used) if b.get("mode") == "claude" else None
        discussion = (claude_file or {}).get("discussion") or []
        msgs = []

        sess = {"pre": "Vorbörse", "regular": "Hauptbörse", "post": "Nachbörse", "closed": "Börse geschlossen"}.get(
            st.get("session"), "Hauptbörse" if st.get("market_open") else "Börse geschlossen")
        msgs.append({"role": "System", "kind": "system",
                     "text": f"Runde eröffnet · {sess} · Eigenkapital ${_f(st.get('equity')):,.0f} · "
                             f"Tages-P&L {_f(st.get('day_pnl_pct')):+.2f}% · "
                             f"{'Paper-Orders aktiv' if b.get('executed') else 'Trockenlauf'} · "
                             f"{'Claude entscheidet' if b.get('mode') == 'claude' else 'regelbasiert'}"})
        m = b.get("macro") or {}
        if m.get("count"):
            hl = m.get("headlines") or []
            msgs.append({"role": "Politik-Analyst", "kind": "macro", "score": None,
                         "text": f"Marktweit: {m['count']} politische Schlagzeilen, Ton-Saldo {m.get('tone', 0):+d}." +
                                 ("" if not hl else " Wichtig: " + " | ".join(hl[:3]))})

        syms = b.get("symbols") or {}
        decs = {d["symbol"]: d for d in b.get("decisions") or []}
        total = {s: sum(_f(o.get("score")) for o in v.get("opinions", [])) for s, v in syms.items()}
        ranked = sorted(syms, key=lambda s: -total[s])
        focus = [s for s in decs if decs[s].get("final") in ("BUY", "CLOSE") or decs[s].get("vetoes")
                 or (decs[s].get("from_claude") and decs[s].get("action") != "HOLD")]
        focus += [s for s in ranked[:TOP_PER_RUN] if s not in focus]
        focus += [s for s in {d.get("symbol") for d in discussion} if s and s not in focus]

        results = {r["symbol"]: r for r in b.get("results") or []}
        for s in focus:
            info = syms.get(s, {})
            msgs.append({"role": "System", "kind": "topic", "symbol": s,
                         "text": f"{s}" + (f" · {info['company']}" if info.get("company") else "") +
                                 (f" · Kurs {info['metrics'].get('price')}" if (info.get("metrics") or {}).get("price") else "")})
            for op in info.get("opinions", []):
                msgs.append({"role": op.get("role"), "kind": "opinion", "symbol": s,
                             "score": round(_f(op.get("score")), 2), "text": _opinion_text(op)})
            for d in discussion:
                if d.get("symbol") == s and d.get("text"):
                    msgs.append({"role": d.get("role") or "Claude", "kind": "discussion", "symbol": s,
                                 "score": d.get("score"), "text": str(d["text"])[:1200]})
            d = decs.get(s)
            if d:
                final = d.get("final", "HOLD")
                text = {"BUY": "Kauf freigegeben", "CLOSE": "Position wird geschlossen"}.get(final, "Halten, kein Trade")
                text += f". {d.get('why', '')}".rstrip()
                if d.get("sizing"):
                    text += f" · {d['sizing']}"
                msgs.append({"role": "Risikomanager", "kind": "veto" if d.get("vetoes") else "decision", "symbol": s,
                             "decision": final, "score": round(_f(d.get("score")), 2), "text": text,
                             "vetoes": d.get("vetoes") or []})
            else:
                msgs.append({"role": "Risikomanager", "kind": "decision", "symbol": s, "decision": "HOLD",
                             "score": round(total.get(s, 0), 2), "text": f"Halten. Summe {total.get(s, 0):+.1f} reicht nicht für einen Trade."})
            if s in results:
                msgs.append({"role": "System", "kind": "exec", "symbol": s, "text": f"Ausführung {results[s]['action']} {s}: {results[s]['status']}"})

        trades = [f"{d['final']} {d['symbol']}" for d in decs.values() if d.get("final") in ("BUY", "CLOSE")]
        msgs.append({"role": "Risikomanager", "kind": "summary",
                     "text": "Ergebnis der Runde: " + (", ".join(trades) if trades else "keine Trades") +
                             f". {len(syms)} Aktien geprüft."})
        runs.append({"id": f.stem, "time": b["time"], "mode": b.get("mode"), "executed": bool(b.get("executed")),
                     "session": sess, "equity": _f(st.get("equity")), "trades": trades,
                     "summary": (claude_file or {}).get("summary"), "messages": msgs})
    return {"generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "roles": ROLES, "runs": runs}


def main():
    sec = Secrets()
    http.set_redactor(sec.redact)
    OUT.mkdir(exist_ok=True)
    try:
        pf = portfolio(sec)
    except Exception as e:
        pf = {"generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
              "errors": [f"Alpaca nicht erreichbar: {sec.redact(str(e))[:200]}"]}
    ch = chat()
    for name, data in (("portfolio.json", pf), ("chat.json", ch)):
        txt = json.dumps(data, ensure_ascii=False, default=str)
        txt = sec.redact(txt)  # Sicherheitsnetz: niemals Keys exportieren
        (OUT / name).write_text(txt, encoding="utf-8")
    print(f"Dashboard-Daten: {OUT}/portfolio.json ({len(pf.get('positions', []))} Positionen, "
          f"{len(pf.get('orders', []))} Orders), chat.json ({len(ch['runs'])} Runden)")
    if pf.get("errors"):
        print("Hinweise:", "; ".join(pf["errors"]))


if __name__ == "__main__":
    main()
