"""Datenquellen: Alpaca (Paper + Marktdaten), Finnhub, FMP, SEC EDGAR.

Jede Quelle ist optional außer Alpaca. Fehler einer Quelle werden gesammelt
und im Bericht vermerkt, statt den Lauf abzubrechen.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from . import http
from .config import Secrets

DATA_URL = "https://data.alpaca.markets"


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


class Alpaca:
    def __init__(self, s: Secrets):
        self.base = s.alpaca_url + "/v2"
        self.h = {"APCA-API-KEY-ID": s.get("ALPACA_API_KEY") or "",
                  "APCA-API-SECRET-KEY": s.get("ALPACA_API_SECRET") or ""}

    # --- Konto ---
    def account(self):
        return http.get(f"{self.base}/account", headers=self.h)

    def clock(self):
        return http.get(f"{self.base}/clock", headers=self.h)

    def positions(self):
        return http.get(f"{self.base}/positions", headers=self.h)

    def open_orders(self):
        return http.get(f"{self.base}/orders", headers=self.h, params={"status": "open", "limit": 100})

    def submit_order(self, order: dict):
        return http.request("POST", f"{self.base}/orders", headers=self.h, body=order, retries=0)

    def close_position(self, symbol: str):
        return http.request("DELETE", f"{self.base}/positions/{symbol}", headers=self.h, retries=0)

    def cancel_orders_for(self, symbol: str):
        for o in self.open_orders() or []:
            if o.get("symbol") == symbol:
                http.request("DELETE", f"{self.base}/orders/{o['id']}", headers=self.h, retries=0)

    # --- Marktdaten ---
    def bars(self, symbols: list[str], timeframe: str, days: int) -> dict[str, list[dict]]:
        start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out: dict[str, list[dict]] = {}
        token = None
        while True:
            r = http.get(f"{DATA_URL}/v2/stocks/bars", headers=self.h, params={
                "symbols": ",".join(symbols), "timeframe": timeframe, "start": start,
                "limit": 10000, "feed": "iex", "adjustment": "split", "page_token": token})
            for sym, bs in (r.get("bars") or {}).items():
                out.setdefault(sym, []).extend(bs)
            token = r.get("next_page_token")
            if not token:
                return out

    def snapshots(self, symbols: list[str]) -> dict:
        return http.get(f"{DATA_URL}/v2/stocks/snapshots", headers=self.h,
                        params={"symbols": ",".join(symbols), "feed": "iex"}) or {}

    def news(self, symbols: list[str], hours: int = 24) -> list[dict]:
        start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = http.get(f"{DATA_URL}/v1beta1/news", headers=self.h,
                     params={"symbols": ",".join(symbols), "start": start, "limit": 50})
        return (r or {}).get("news", [])

    def most_actives(self, top: int) -> list[str]:
        r = http.get(f"{DATA_URL}/v1beta1/screener/stocks/most-actives", headers=self.h,
                     params={"by": "volume", "top": top})
        return [x["symbol"] for x in (r or {}).get("most_actives", [])]


class Finnhub:
    URL = "https://finnhub.io/api/v1"

    def __init__(self, s: Secrets):
        self.key = s.get("FINNHUB_API_KEY")

    @property
    def enabled(self):
        return bool(self.key)

    def _get(self, path: str, **params):
        params["token"] = self.key
        return http.get(f"{self.URL}{path}", params=params)

    def company_news(self, sym: str, days: int = 2):
        return self._get("/company-news", symbol=sym, **{"from": str(_today() - dt.timedelta(days=days)), "to": str(_today())}) or []

    def insider_transactions(self, sym: str, days: int = 30):
        r = self._get("/stock/insider-transactions", symbol=sym, **{"from": str(_today() - dt.timedelta(days=days)), "to": str(_today())})
        return (r or {}).get("data", [])

    def recommendation(self, sym: str):
        return self._get("/stock/recommendation", symbol=sym) or []

    def basic_metrics(self, sym: str) -> dict:
        r = self._get("/stock/metric", symbol=sym, metric="all")
        m = (r or {}).get("metric", {})
        return {"peRatioTTM": m.get("peTTM") or m.get("peBasicExclExtraTTM"), "beta": m.get("beta")}

    def earnings_calendar(self, days: int = 3):
        r = self._get("/calendar/earnings", **{"from": str(_today()), "to": str(_today() + dt.timedelta(days=days))})
        return (r or {}).get("earningsCalendar", [])

    def general_news(self):
        return self._get("/news", category="general") or []

    def congressional_trading(self, sym: str):
        r = self._get("/stock/congressional-trading", symbol=sym,
                      **{"from": str(_today() - dt.timedelta(days=60)), "to": str(_today())})
        return (r or {}).get("data", [])


class FMP:
    URL = "https://financialmodelingprep.com/stable"

    def __init__(self, s: Secrets):
        self.key = s.get("FMP_API_KEY")

    @property
    def enabled(self):
        return bool(self.key)

    def _get(self, path: str, **params):
        params["apikey"] = self.key
        return http.get(f"{self.URL}{path}", params=params)

    def profile(self, sym: str) -> dict:
        r = self._get("/profile", symbol=sym)
        return r[0] if isinstance(r, list) and r else {}

    def ratios_ttm(self, sym: str) -> dict:
        r = self._get("/ratios-ttm", symbol=sym)
        return r[0] if isinstance(r, list) and r else {}

    def senate_trades(self, sym: str):
        return self._get("/senate-trades", symbol=sym) or []

    def house_trades(self, sym: str):
        return self._get("/house-trades", symbol=sym) or []


class SecEdgar:
    def __init__(self, s: Secrets):
        self.ua = s.get("SEC_EDGAR_USER_AGENT")
        self._cik: dict[str, str] | None = None

    @property
    def enabled(self):
        return bool(self.ua)

    def _h(self):
        return {"User-Agent": self.ua, "Accept-Encoding": "identity"}

    def cik(self, sym: str) -> str | None:
        if self._cik is None:
            r = http.get("https://www.sec.gov/files/company_tickers.json", headers=self._h())
            self._cik = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in (r or {}).values()}
        return self._cik.get(sym.upper())

    def recent_filings(self, sym: str, days: int = 14) -> list[dict[str, Any]]:
        cik = self.cik(sym)
        if not cik:
            return []
        r = http.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=self._h())
        rec = (r or {}).get("filings", {}).get("recent", {})
        cutoff = str(_today() - dt.timedelta(days=days))
        out = []
        for form, date, acc in zip(rec.get("form", []), rec.get("filingDate", []), rec.get("accessionNumber", [])):
            if date < cutoff:
                break
            if form in ("4", "8-K", "SC 13D", "SC 13G", "13D", "13G"):
                out.append({"form": form, "date": date, "accession": acc})
        return out
