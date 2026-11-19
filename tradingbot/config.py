"""Konfiguration: Keys laden, Paper-Endpunkt erzwingen, Risiko-Parameter.

Keys werden aus keys.env.txt bzw. .env im Projektordner gelesen (nicht-leere
Werte gewinnen, echte Umgebungsvariablen haben Vorrang). Werte werden nie
ausgegeben.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY_FILES = [ROOT / "keys.env.txt", ROOT / ".env"]
SETTINGS_FILE = ROOT / "settings.json"

PAPER_HOST = "paper-api.alpaca.markets"


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        v = v.strip().strip('"').strip("'")
        if v:
            out[k] = v
    return out


def load_keys() -> dict[str, str]:
    keys: dict[str, str] = {}
    for f in KEY_FILES:
        for k, v in _parse_env_file(f).items():
            keys.setdefault(k, v)
    for k, v in os.environ.items():
        if v and (k.startswith(("ALPACA_", "FINNHUB_", "FMP_", "SEC_", "REDDIT_", "TWELVE_", "ALPHA_", "SENTIMENT_", "FRED_"))):
            keys[k] = v
    return keys


def normalize_alpaca_url(url: str | None) -> str:
    """Akzeptiert mit/ohne /v2, erzwingt den Paper-Host."""
    url = (url or f"https://{PAPER_HOST}").strip().rstrip("/")
    if url.endswith("/v2"):
        url = url[:-3]
    if PAPER_HOST not in url:
        raise SystemExit(
            "ABBRUCH: ALPACA_BASE_URL zeigt nicht auf den Paper-Endpunkt "
            f"({PAPER_HOST}). Dieser Bot handelt ausschließlich im Paper-Konto."
        )
    return url


@dataclass
class Settings:
    # Universum
    watchlist: list[str] = field(default_factory=lambda: [
        "AAPL", "MSFT", "NVDA", "AMD", "AMZN", "META", "GOOGL", "TSLA",
        "PLTR", "AVGO", "JPM", "XOM", "LMT", "SMCI", "COIN",
    ])
    add_most_actives: int = 5          # zusätzlich die N aktivsten Aktien (Alpaca Screener)
    max_symbols: int = 20
    min_price: float = 5.0             # keine Penny-Stocks
    min_dollar_volume: float = 50_000_000  # Ø Tagesumsatz in $ (Gesamtmarkt, SIP-Daten)
    # Risiko
    max_position_pct: float = 0.10     # max. Anteil des Eigenkapitals je Position
    max_open_positions: int = 5
    max_daily_loss_pct: float = 0.03   # ab -3 % Tagesverlust keine neuen Käufe
    stop_loss_pct: float = 0.04
    take_profit_pct: float = 0.08
    min_total_score: float = 3.0       # Summe der Rollen-Scores für Kauf
    min_agreeing_roles: int = 2        # mind. so viele Rollen positiv
    sell_score: float = -2.0           # Summe <= diesem Wert -> bestehende Position schließen
    flatten_minutes_before_close: int = 15  # Daytrading: kurz vor Schluss alles glattstellen
    allow_short: bool = False
    # Vor- und Nachbörse (04:00–09:30 und 16:00–20:00 ET = 10:00–15:30 und 22:00–02:00 MEZ)
    extended_hours: bool = True
    ext_position_factor: float = 0.5   # halbe Positionsgröße außerhalb der Hauptbörse
    ext_limit_offset_pct: float = 0.002  # Limit 0,2 % über Brief (Kauf) bzw. unter Geld (Verkauf)
    ext_max_spread_pct: float = 0.3    # kein Kauf, wenn Geld-/Brief-Spanne größer
    ext_extra_score: float = 0.5       # höhere Kaufschwelle außerhalb der Hauptbörse


def load_settings() -> Settings:
    s = Settings()
    if SETTINGS_FILE.exists():
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        for k, v in data.items():
            if hasattr(s, k):
                setattr(s, k, v)
    return s


class Secrets:
    """Hält die Keys und kann Texte von Key-Werten säubern."""

    def __init__(self) -> None:
        self.k = load_keys()
        self.alpaca_url = normalize_alpaca_url(self.k.get("ALPACA_BASE_URL"))

    def get(self, name: str) -> str | None:
        return self.k.get(name)

    def has(self, name: str) -> bool:
        return bool(self.k.get(name))

    def redact(self, text: str) -> str:
        for name, v in self.k.items():
            if len(v) >= 8 and name != "ALPACA_BASE_URL":
                text = text.replace(v, "***")
        return text
