"""Die fünf Rollen. Jede Rolle bewertet ein Symbol mit Score -2 (stark negativ)
bis +2 (stark positiv) und nennt ihre Gründe. Der Risikomanager entscheidet final.

Die Bewertung ist regelbasiert (keine kostenpflichtige KI-API nötig). Claude
kann im Projekt das Briefing lesen und per `apply` eigene Entscheidungen
einspeisen; die harten Risiko-Regeln gelten trotzdem.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

POS_WORDS = r"beat|beats|surge|soar|jump|record|upgrade|raises guidance|raised guidance|strong demand|partnership|buyback|outperform|approval|wins contract|tops estimates|rally|bullish|expands"
NEG_WORDS = r"miss|misses|plunge|drop|falls|downgrade|cuts guidance|lowered guidance|lawsuit|probe|investigation|recall|layoffs|fraud|bearish|warning|delay|halt|sec charges|antitrust|tariff"
POLITICS_WORDS = r"congress|senate|white house|president|tariff|sanction|export control|fed |federal reserve|rate cut|rate hike|regulation|regulator|antitrust|ftc|doj|subsid|chips act|defense budget|election|government shutdown|trade war|executive order"


def clamp(x: float, lo=-2.0, hi=2.0) -> float:
    return max(lo, min(hi, x))


def text_tone(text: str) -> int:
    t = text.lower()
    return len(re.findall(POS_WORDS, t)) - len(re.findall(NEG_WORDS, t))


@dataclass
class Opinion:
    role: str
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    data_ok: bool = True

    def as_dict(self):
        return {"role": self.role, "score": round(self.score, 2), "reasons": self.reasons, "data_ok": self.data_ok}


# ---------------- Indikatoren ----------------
def sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def rsi(xs, n=14):
    if len(xs) <= n:
        return None
    gains = losses = 0.0
    for a, b in zip(xs[-n - 1:-1], xs[-n:]):
        d = b - a
        gains += max(d, 0)
        losses += max(-d, 0)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - 100 / (1 + rs)


def quant_metrics(daily: list[dict], intraday: list[dict]) -> dict:
    closes = [b["c"] for b in daily]
    vols = [b["v"] for b in daily]
    m: dict = {}
    if closes:
        m["last_close"] = closes[-1]
        m["sma20"] = sma(closes, 20)
        m["sma50"] = sma(closes, 50)
        m["rsi14"] = rsi(closes)
        m["mom5d_pct"] = (closes[-1] / closes[-6] - 1) * 100 if len(closes) > 5 else None
        m["vol_ratio"] = (vols[-1] / (sum(vols[-21:-1]) / 20)) if len(vols) > 21 and sum(vols[-21:-1]) else None
    if intraday:
        today = intraday[-1]["t"][:10]
        tb = [b for b in intraday if b["t"][:10] == today]
        if tb:
            pv = sum(b["c"] * b["v"] for b in tb)
            v = sum(b["v"] for b in tb)
            m["price"] = tb[-1]["c"]
            m["vwap_today"] = pv / v if v else None
            m["day_change_pct"] = (tb[-1]["c"] / tb[0]["o"] - 1) * 100
            ic = [b["c"] for b in intraday]
            m["rsi14_15m"] = rsi(ic)
    m.setdefault("price", m.get("last_close"))
    return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in m.items()}


# ---------------- Rollen ----------------
def quant(sym: str, m: dict) -> Opinion:
    o = Opinion("Quant")
    if not m.get("price"):
        o.data_ok = False
        o.reasons.append("keine Kursdaten")
        return o
    s = 0.0
    p, s20, s50 = m.get("price"), m.get("sma20"), m.get("sma50")
    if s20 and s50:
        if p > s20 > s50:
            s += 1; o.reasons.append("Aufwärtstrend (Kurs > SMA20 > SMA50)")
        elif p < s20 < s50:
            s -= 1; o.reasons.append("Abwärtstrend (Kurs < SMA20 < SMA50)")
    r = m.get("rsi14")
    if r is not None:
        if r > 75:
            s -= 0.75; o.reasons.append(f"RSI {r:.0f} überkauft")
        elif r < 30:
            s += 0.5; o.reasons.append(f"RSI {r:.0f} überverkauft (Rebound möglich)")
    vw = m.get("vwap_today")
    if vw:
        if p > vw * 1.002:
            s += 0.5; o.reasons.append("Kurs über Tages-VWAP")
        elif p < vw * 0.998:
            s -= 0.5; o.reasons.append("Kurs unter Tages-VWAP")
    mom = m.get("mom5d_pct")
    if mom is not None:
        if mom > 3:
            s += 0.5; o.reasons.append(f"5-Tage-Momentum +{mom:.1f}%")
        elif mom < -3:
            s -= 0.5; o.reasons.append(f"5-Tage-Momentum {mom:.1f}%")
    vr = m.get("vol_ratio")
    if vr and vr > 1.5:
        s *= 1.25; o.reasons.append(f"Volumen {vr:.1f}x über Schnitt (verstärkt Signal)")
    o.score = clamp(s)
    return o


def analyst(sym: str, news: list[dict], rec: list[dict], ratios: dict, profile: dict, earnings_soon: bool) -> Opinion:
    o = Opinion("Analyst")
    s = 0.0
    tones = [text_tone((n.get("headline") or "") + " " + (n.get("summary") or "")) for n in news]
    if tones:
        t = sum(tones)
        s += clamp(t / 3, -1.5, 1.5)
        o.reasons.append(f"{len(news)} News (Ton-Saldo {t:+d})")
        top = [n.get("headline", "")[:110] for n in news[:2]]
        o.reasons += [f"„{h}“" for h in top if h]
    else:
        o.reasons.append("keine aktuellen News")
    if rec:
        r0 = rec[0]
        buy = r0.get("strongBuy", 0) + r0.get("buy", 0)
        sell = r0.get("strongSell", 0) + r0.get("sell", 0)
        tot = buy + sell + r0.get("hold", 0)
        if tot:
            ratio = (buy - sell) / tot
            s += 0.5 * ratio
            o.reasons.append(f"Analystenkonsens {buy} Buy / {r0.get('hold', 0)} Hold / {sell} Sell")
    pe = ratios.get("priceToEarningsRatioTTM") or ratios.get("peRatioTTM")
    if isinstance(pe, (int, float)) and pe > 0:
        if pe > 80:
            s -= 0.25; o.reasons.append(f"KGV {pe:.0f} sehr hoch")
        elif pe < 15:
            s += 0.25; o.reasons.append(f"KGV {pe:.0f} günstig")
    if earnings_soon:
        o.reasons.append("Quartalszahlen in den nächsten Tagen (Event-Risiko)")
    if profile.get("sector"):
        o.reasons.append(f"Sektor: {profile['sector']}")
    o.score = clamp(s)
    return o


def insider(sym: str, txs: list[dict], filings: list[dict]) -> Opinion:
    o = Opinion("Insider-Beobachter")
    buys = [t for t in txs if t.get("transactionCode") == "P"]
    sells = [t for t in txs if t.get("transactionCode") == "S"]
    buy_val = sum(abs(t.get("change") or 0) * (t.get("transactionPrice") or 0) for t in buys)
    sell_val = sum(abs(t.get("change") or 0) * (t.get("transactionPrice") or 0) for t in sells)
    s = 0.0
    if buys:
        s += min(2.0, 0.75 + len({t.get("name") for t in buys}) * 0.5)
        o.reasons.append(f"{len(buys)} Insider-KÄUFE (30 T., ~${buy_val/1e6:.2f} Mio.)")
    if sells:
        if sell_val > 5e6 or len(sells) >= 6:
            s -= 0.75
        o.reasons.append(f"{len(sells)} Insider-Verkäufe (~${sell_val/1e6:.2f} Mio., oft planmäßig)")
    f4 = [f for f in filings if f["form"] == "4"]
    k8 = [f for f in filings if f["form"] == "8-K"]
    act = [f for f in filings if f["form"] not in ("4", "8-K")]
    if f4:
        o.reasons.append(f"{len(f4)} Form-4-Meldungen bei der SEC (14 T.)")
    if k8:
        o.reasons.append(f"8-K am {k8[0]['date']} (wesentliches Ereignis)")
    if act:
        s += 0.5; o.reasons.append(f"Beteiligungsmeldung {act[0]['form']} am {act[0]['date']}")
    if not (txs or filings):
        o.reasons.append("keine Insider-Aktivität")
    o.score = clamp(s)
    return o


def politics(sym: str, company: str, pol_trades: list[dict], general_news: list[dict], sector: str) -> Opinion:
    o = Opinion("Politik-Analyst")
    s = 0.0
    if pol_trades:
        b = sum(1 for t in pol_trades if "purchase" in str(t.get("transaction") or t.get("type") or t.get("transactionType") or "").lower())
        sl = sum(1 for t in pol_trades if "sale" in str(t.get("transaction") or t.get("type") or t.get("transactionType") or "").lower())
        s += clamp((b - sl) * 0.5, -1.0, 1.0)
        o.reasons.append(f"Politiker-Trades (60 T.): {b} Käufe / {sl} Verkäufe")
    names = {sym.lower()}
    if company:
        names.add(company.lower().split(",")[0].replace(" inc.", "").replace(" inc", "").replace(" corporation", "").strip())
    hits = []
    for n in general_news:
        txt = ((n.get("headline") or "") + " " + (n.get("summary") or "")).lower()
        if re.search(POLITICS_WORDS, txt) and any(re.search(rf"\b{re.escape(x)}\b", txt) for x in names if len(x) > 2):
            hits.append(n)
    if hits:
        t = sum(text_tone((h.get("headline") or "") + " " + (h.get("summary") or "")) for h in hits)
        s += clamp(t / 2, -1.5, 1.0)
        o.reasons.append(f"{len(hits)} politische Meldungen zum Unternehmen (Ton {t:+d})")
        o.reasons.append(f"„{hits[0].get('headline', '')[:110]}“")
    if not o.reasons:
        o.reasons.append("keine politischen Signale")
    o.score = clamp(s)
    return o


def macro_politics(general_news: list[dict]) -> dict:
    """Marktweite Politik-Lage (für den Risikomanager)."""
    hits = [n for n in general_news if re.search(POLITICS_WORDS, ((n.get("headline") or "") + " " + (n.get("summary") or "")).lower())]
    tone = sum(text_tone((n.get("headline") or "") + " " + (n.get("summary") or "")) for n in hits)
    return {"count": len(hits), "tone": tone, "headlines": [n.get("headline", "")[:140] for n in hits[:6]]}
