"""Minimaler HTTP-Client (nur Standardbibliothek), mit Key-Schwärzung in Fehlern."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request


class ApiError(Exception):
    pass


_REDACTOR = None


def set_redactor(fn) -> None:
    global _REDACTOR
    _REDACTOR = fn


def _r(text: str) -> str:
    return _REDACTOR(text) if _REDACTOR else text


def request(method: str, url: str, params: dict | None = None, headers: dict | None = None,
            body: dict | None = None, timeout: int = 20, retries: int = 2):
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(clean)
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Accept": "application/json", "User-Agent": "paper-tradingbot/1.0"}
    hdrs.update(headers or {})
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", errors="replace")[:300]
            last = ApiError(_r(f"HTTP {e.code} bei {urllib.parse.urlsplit(url).netloc}{urllib.parse.urlsplit(url).path}: {msg}"))
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep((10 if e.code == 429 else 1.5) * (attempt + 1))
                continue
            raise last
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = ApiError(_r(f"Netzwerkfehler bei {urllib.parse.urlsplit(url).netloc}: {e}"))
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise last
    raise last  # pragma: no cover


def get(url, **kw):
    return request("GET", url, **kw)
