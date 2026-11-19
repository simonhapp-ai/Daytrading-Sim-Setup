"""Risikomanager: fasst die Rollen zusammen, setzt harte Regeln durch, hat Veto.

Handelssitzungen (US-Ostküstenzeit / deutsche Zeit, Normalfall):
  pre      04:00–09:30 ET  /  10:00–15:30 MEZ   Vorbörse (nur Limit-Orders, extended_hours)
  regular  09:30–16:00 ET  /  15:30–22:00 MEZ   Hauptbörse (Bracket-Orders mit Stop/Ziel)
  post     16:00–20:00 ET  /  22:00–02:00 MEZ   Nachbörse (nur Limit-Orders, extended_hours)
Die genauen Zeiten (Feiertage, verkürzte Tage) kommen aus dem Alpaca-Kalender.
"""
from __future__ import annotations

import datetime as dt
import math

from .config import Settings

SESSION_DE = {"pre": "Vorbörse", "regular": "Hauptbörse", "post": "Nachbörse", "closed": "geschlossen"}


def _hm(date: str, hhmm: str, tz) -> dt.datetime:
    hhmm = hhmm.replace(":", "")
    d = dt.date.fromisoformat(date)
    return dt.datetime(d.year, d.month, d.day, int(hhmm[:2]), int(hhmm[2:]), tzinfo=tz)


def session_info(clock: dict, calendar: list[dict]) -> dict:
    """Bestimmt die aktuelle Sitzung und die Minuten bis zu ihrem Ende."""
    now = dt.datetime.fromisoformat(clock["timestamp"])
    tz = now.tzinfo
    today = now.date().isoformat()
    cal = next((c for c in calendar or [] if c.get("date") == today), None)
    if not cal:
        return {"session": "closed", "minutes_to_session_end": None, "minutes_to_regular_close": None}
    s_open = _hm(today, cal.get("session_open", "0400"), tz)
    r_open = _hm(today, cal["open"], tz)
    r_close = _hm(today, cal["close"], tz)
    s_close = _hm(today, cal.get("session_close", "2000"), tz)
    if clock.get("is_open") or r_open <= now < r_close:
        sess, end = "regular", r_close
    elif s_open <= now < r_open:
        sess, end = "pre", r_open
    elif r_close <= now < s_close:
        sess, end = "post", s_close
    else:
        return {"session": "closed", "minutes_to_session_end": None, "minutes_to_regular_close": None}
    return {"session": sess, "minutes_to_session_end": (end - now).total_seconds() / 60,
            "minutes_to_regular_close": (r_close - now).total_seconds() / 60}


def portfolio_state(account: dict, positions: list[dict], clock: dict, calendar: list[dict], s: Settings) -> dict:
    equity = float(account.get("equity") or 0)
    last = float(account.get("last_equity") or equity or 1)
    si = session_info(clock, calendar)
    allowed = {"regular"} | ({"pre", "post"} if s.extended_hours else set())
    return {
        "equity": equity,
        "cash": float(account.get("cash") or 0),
        "buying_power": float(account.get("buying_power") or 0),
        "day_pnl_pct": (equity / last - 1) * 100 if last else 0.0,
        "open_positions": {p["symbol"]: p for p in positions},
        "session": si["session"],
        "extended": si["session"] in ("pre", "post"),
        "market_open": si["session"] in allowed,     # darf in dieser Sitzung gehandelt werden?
        "minutes_to_close": si["minutes_to_session_end"],
        "blocked": bool(account.get("trading_blocked") or account.get("account_blocked")),
        "next_open": clock.get("next_open"),
    }


def spread_pct(q: dict | None) -> float | None:
    if not q or not q.get("bid") or not q.get("ask") or q["ask"] < q["bid"]:
        return None
    mid = (q["bid"] + q["ask"]) / 2
    return (q["ask"] - q["bid"]) / mid * 100


def _buy_order(sym: str, price: float, st: dict, s: Settings, size_factor: float = 1.0,
               quote: dict | None = None) -> tuple[dict | None, str]:
    factor = max(0.1, min(1.0, size_factor))
    if st["extended"]:
        factor *= s.ext_position_factor
    budget = min(st["equity"] * s.max_position_pct * factor, st["buying_power"] * 0.95)
    if st["extended"]:
        # Vor-/Nachbörse: nur Limit-Order, knapp über dem Briefkurs, kein Bracket möglich
        limit = round(quote["ask"] * (1 + s.ext_limit_offset_pct), 2)
        qty = math.floor(budget / limit)
        if qty < 1:
            return None, "Budget reicht nicht für 1 Aktie"
        return {"symbol": sym, "qty": str(qty), "side": "buy", "type": "limit", "limit_price": f"{limit:.2f}",
                "time_in_force": "day", "extended_hours": True}, \
            (f"{qty} Stk. Limit {limit:.2f} $ (~${qty * limit:,.0f}, {SESSION_DE[st['session']]}, halbe Größe), "
             f"Stop {s.stop_loss_pct:.0%}/Ziel {s.take_profit_pct:.0%} überwacht der Bot selbst")
    qty = math.floor(budget / price) if price else 0
    if qty < 1:
        return None, "Budget reicht nicht für 1 Aktie"
    return {
        "symbol": sym, "qty": str(qty), "side": "buy", "type": "market", "time_in_force": "day",
        "order_class": "bracket",
        "take_profit": {"limit_price": f"{price * (1 + s.take_profit_pct):.2f}"},
        "stop_loss": {"stop_price": f"{price * (1 - s.stop_loss_pct):.2f}"},
    }, f"{qty} Stk. (~${qty * price:,.0f}), Stop {s.stop_loss_pct:.0%}, Ziel {s.take_profit_pct:.0%}"


def close_order(sym: str, st: dict, s: Settings, quote: dict | None) -> tuple[dict | None, str | None]:
    """In der Hauptbörse schließt Alpaca per Market-Order (kein Order-Objekt nötig).
    Außerhalb nur per Limit-Order knapp unter dem Geldkurs."""
    if not st["extended"]:
        return None, None
    pos = st["open_positions"][sym]
    if not quote or not quote.get("bid"):
        return None, "kein aktueller Geldkurs für Limit-Verkauf"
    limit = round(quote["bid"] * (1 - s.ext_limit_offset_pct), 2)
    qty = abs(float(pos["qty"]))
    return {"symbol": sym, "qty": f"{qty:g}", "side": "sell" if float(pos["qty"]) > 0 else "buy",
            "type": "limit", "limit_price": f"{limit:.2f}", "time_in_force": "day", "extended_hours": True}, None


def global_vetoes(st: dict, s: Settings, macro: dict) -> list[str]:
    v = []
    if st["blocked"]:
        v.append("Konto ist für Handel gesperrt")
    if st["day_pnl_pct"] <= -s.max_daily_loss_pct * 100:
        v.append(f"Tagesverlust {st['day_pnl_pct']:.2f}% erreicht Limit, keine neuen Käufe")
    mtc = st["minutes_to_close"]
    if st["session"] in ("regular", "post") and mtc is not None and mtc <= s.flatten_minutes_before_close + 30:
        v.append(f"nur noch {mtc:.0f} Min. bis Ende der {SESSION_DE[st['session']]}, keine neuen Positionen")
    if len(st["open_positions"]) >= s.max_open_positions:
        v.append(f"bereits {len(st['open_positions'])} Positionen offen (Max. {s.max_open_positions})")
    return v


def decide(symbols: list[str], opinions: dict[str, list], metrics: dict[str, dict], st: dict, s: Settings,
           macro: dict, earnings_soon: set[str], quotes: dict | None = None) -> list[dict]:
    """Regelbasierte Entscheidung aus den Rollen-Scores."""
    proposals = []
    for sym in symbols:
        ops = opinions.get(sym, [])
        total = sum(o.score for o in ops)
        positive = sum(1 for o in ops if o.score > 0.25)
        q = next((o for o in ops if o.role == "Quant"), None)
        held = sym in st["open_positions"]
        if held and total <= s.sell_score:
            proposals.append({"symbol": sym, "action": "SELL", "score": total, "why": f"Gesamtscore {total:+.1f} unter Verkaufsschwelle"})
        elif not held and total >= s.min_total_score and positive >= s.min_agreeing_roles:
            proposals.append({"symbol": sym, "action": "BUY", "score": total, "size_factor": min(1.0, total / 5),
                              "why": f"Gesamtscore {total:+.1f}, {positive} Rollen positiv"})
        else:
            proposals.append({"symbol": sym, "action": "HOLD", "score": total, "why": f"Gesamtscore {total:+.1f}, {positive} Rollen positiv"})
        proposals[-1]["quant_score"] = q.score if q else 0
    return enforce(proposals, metrics, st, s, macro, earnings_soon, quotes)


def enforce(proposals: list[dict], metrics: dict[str, dict], st: dict, s: Settings, macro: dict,
            earnings_soon: set[str], quotes: dict | None = None) -> list[dict]:
    """Harte Regeln + Veto. Gilt für regelbasierte UND von Claude gelieferte Entscheidungen."""
    quotes = quotes or {}
    gv = global_vetoes(st, s, macro)
    slots = s.max_open_positions - len(st["open_positions"])
    min_score = s.min_total_score + (1.0 if macro.get("tone", 0) <= -4 else 0.0) \
        + (s.ext_extra_score if st["extended"] else 0.0)
    out = []
    handled = set()

    def add_close(sym, why):
        order, err = close_order(sym, st, s, quotes.get(sym))
        d = {"symbol": sym, "action": "CLOSE", "why": why, "vetoes": []}
        if err:
            d.update(final="HOLD", vetoes=[err])
        else:
            d["final"] = "CLOSE"
            if order:
                d["order"] = order
        out.append(d)
        handled.add(sym)

    # 1) Daytrading: kurz vor Ende der Haupt- bzw. Nachbörse alles glattstellen
    mtc = st["minutes_to_close"]
    if st["market_open"] and st["session"] in ("regular", "post") and mtc is not None and mtc <= s.flatten_minutes_before_close:
        for sym in st["open_positions"]:
            add_close(sym, f"Daytrading: glattstellen vor Ende der {SESSION_DE[st['session']]}")
    # 2) Eigener Stop-Loss / Gewinnziel (wichtig für Vor-/Nachbörse, dort gibt es keine Bracket-Orders)
    for sym, p in st["open_positions"].items():
        if sym in handled:
            continue
        plpc = float(p.get("unrealized_plpc") or 0)
        if plpc <= -s.stop_loss_pct:
            add_close(sym, f"Stop-Loss erreicht ({plpc*100:+.1f}%)")
        elif plpc >= s.take_profit_pct:
            add_close(sym, f"Gewinnziel erreicht ({plpc*100:+.1f}%)")

    for p in sorted(proposals, key=lambda x: -x.get("score", 0)):
        sym = p["symbol"]
        if sym in handled:
            continue
        d = dict(p)
        d["vetoes"] = []
        price = (metrics.get(sym) or {}).get("price")
        q = quotes.get(sym)
        if p["action"] == "BUY":
            v = list(gv)
            if sym in st["open_positions"]:
                v.append("Position bereits vorhanden")
            if sym in earnings_soon:
                v.append("Quartalszahlen stehen an (Event-Risiko)")
            if p.get("quant_score", 0) < 0 and not p.get("from_claude"):
                v.append("Quant-Signal negativ")
            if not p.get("from_claude") and p.get("score", 0) < min_score:
                v.append(f"Score unter Mindestwert {min_score:.1f}"
                         + (" (Vor-/Nachbörse verlangt mehr)" if st["extended"] else " (Makro-Politik negativ)"))
            if not price:
                v.append("kein Kurs verfügbar")
            if st["extended"]:
                sp = spread_pct(q)
                if sp is None:
                    v.append("kein verwertbarer Geld-/Briefkurs in der Vor-/Nachbörse")
                elif sp > s.ext_max_spread_pct:
                    v.append(f"Spread {sp:.2f}% zu groß (Max. {s.ext_max_spread_pct}%)")
            if slots <= 0:
                v.append("keine freien Positions-Slots")
            if v:
                d["final"] = "HOLD"
                d["vetoes"] = v
            else:
                order, info = _buy_order(sym, price, st, s, p.get("size_factor", 1.0), q)
                if order:
                    d["final"] = "BUY"
                    d["order"] = order
                    d["sizing"] = info
                    slots -= 1
                else:
                    d["final"] = "HOLD"
                    d["vetoes"] = [info]
            out.append(d)
        elif p["action"] in ("SELL", "CLOSE"):
            if sym in st["open_positions"]:
                add_close(sym, p.get("why", ""))
            else:
                d["final"] = "HOLD"
                d["vetoes"] = ["keine Position zum Verkaufen" + ("" if s.allow_short else " (Leerverkauf deaktiviert)")]
                out.append(d)
        else:
            d["final"] = "HOLD"
            out.append(d)
    return out
