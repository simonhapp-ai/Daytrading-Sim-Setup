"""Risikomanager: fasst die Rollen zusammen, setzt harte Regeln durch, hat Veto."""
from __future__ import annotations

import datetime as dt
import math

from .config import Settings


def _minutes_to_close(clock: dict) -> float | None:
    try:
        now = dt.datetime.fromisoformat(clock["timestamp"])
        close = dt.datetime.fromisoformat(clock["next_close"])
        return (close - now).total_seconds() / 60
    except Exception:
        return None


def portfolio_state(account: dict, positions: list[dict], clock: dict) -> dict:
    equity = float(account.get("equity") or 0)
    last = float(account.get("last_equity") or equity or 1)
    return {
        "equity": equity,
        "cash": float(account.get("cash") or 0),
        "buying_power": float(account.get("buying_power") or 0),
        "day_pnl_pct": (equity / last - 1) * 100 if last else 0.0,
        "open_positions": {p["symbol"]: p for p in positions},
        "market_open": bool(clock.get("is_open")),
        "minutes_to_close": _minutes_to_close(clock),
        "blocked": bool(account.get("trading_blocked") or account.get("account_blocked")),
    }


def _buy_order(sym: str, price: float, st: dict, s: Settings, size_factor: float = 1.0) -> tuple[dict | None, str]:
    budget = st["equity"] * s.max_position_pct * max(0.1, min(1.0, size_factor))
    budget = min(budget, st["buying_power"] * 0.95)
    qty = math.floor(budget / price) if price else 0
    if qty < 1:
        return None, "Budget reicht nicht für 1 Aktie"
    return {
        "symbol": sym, "qty": str(qty), "side": "buy", "type": "market", "time_in_force": "day",
        "order_class": "bracket",
        "take_profit": {"limit_price": f"{price * (1 + s.take_profit_pct):.2f}"},
        "stop_loss": {"stop_price": f"{price * (1 - s.stop_loss_pct):.2f}"},
    }, f"{qty} Stk. (~${qty * price:,.0f}), Stop {s.stop_loss_pct:.0%}, Ziel {s.take_profit_pct:.0%}"


def global_vetoes(st: dict, s: Settings, macro: dict) -> list[str]:
    v = []
    if st["blocked"]:
        v.append("Konto ist für Handel gesperrt")
    if st["day_pnl_pct"] <= -s.max_daily_loss_pct * 100:
        v.append(f"Tagesverlust {st['day_pnl_pct']:.2f}% erreicht Limit, keine neuen Käufe")
    mtc = st["minutes_to_close"]
    if st["market_open"] and mtc is not None and mtc <= s.flatten_minutes_before_close + 30:
        v.append(f"nur noch {mtc:.0f} Min. bis Börsenschluss, keine neuen Positionen")
    if len(st["open_positions"]) >= s.max_open_positions:
        v.append(f"bereits {len(st['open_positions'])} Positionen offen (Max. {s.max_open_positions})")
    return v


def decide(symbols: list[str], opinions: dict[str, list], metrics: dict[str, dict], st: dict, s: Settings,
           macro: dict, earnings_soon: set[str]) -> list[dict]:
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
    return enforce(proposals, metrics, st, s, macro, earnings_soon)


def enforce(proposals: list[dict], metrics: dict[str, dict], st: dict, s: Settings, macro: dict,
            earnings_soon: set[str]) -> list[dict]:
    """Harte Regeln + Veto. Gilt für regelbasierte UND von Claude gelieferte Entscheidungen."""
    gv = global_vetoes(st, s, macro)
    slots = s.max_open_positions - len(st["open_positions"])
    min_score = s.min_total_score + (1.0 if macro.get("tone", 0) <= -4 else 0.0)
    out = []
    # Flatten kurz vor Schluss (Daytrading)
    mtc = st["minutes_to_close"]
    flatten = st["market_open"] and mtc is not None and mtc <= s.flatten_minutes_before_close
    handled = set()
    if flatten:
        for sym in st["open_positions"]:
            out.append({"symbol": sym, "action": "CLOSE", "final": "CLOSE", "why": "Daytrading: glattstellen vor Börsenschluss", "vetoes": []})
            handled.add(sym)

    for p in sorted(proposals, key=lambda x: -x.get("score", 0)):
        sym = p["symbol"]
        if sym in handled:
            continue
        d = dict(p)
        d["vetoes"] = []
        price = (metrics.get(sym) or {}).get("price")
        if p["action"] == "BUY":
            v = list(gv)
            if sym in st["open_positions"]:
                v.append("Position bereits vorhanden")
            if sym in earnings_soon:
                v.append("Quartalszahlen stehen an (Event-Risiko)")
            if p.get("quant_score", 0) < 0 and not p.get("from_claude"):
                v.append("Quant-Signal negativ")
            if not p.get("from_claude") and p.get("score", 0) < min_score:
                v.append(f"Score unter Mindestwert {min_score:.1f} (Makro-Politik negativ)")
            if not price:
                v.append("kein Kurs verfügbar")
            if slots <= 0:
                v.append("keine freien Positions-Slots")
            if v:
                d["final"] = "HOLD"
                d["vetoes"] = v
            else:
                order, info = _buy_order(sym, price, st, s, p.get("size_factor", 1.0))
                if order:
                    d["final"] = "BUY"
                    d["order"] = order
                    d["sizing"] = info
                    slots -= 1
                else:
                    d["final"] = "HOLD"
                    d["vetoes"] = [info]
        elif p["action"] in ("SELL", "CLOSE"):
            if sym in st["open_positions"]:
                d["final"] = "CLOSE"
            else:
                d["final"] = "HOLD"
                d["vetoes"] = ["keine Position zum Verkaufen" + ("" if s.allow_short else " (Leerverkauf deaktiviert)")]
        else:
            d["final"] = "HOLD"
        out.append(d)
    return out
