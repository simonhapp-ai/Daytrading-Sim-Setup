# Paper-Trading-Agentengruppe (US-Aktien)

Fünf Rollen bewerten stündlich eine Auswahl von US-Aktien, ein Risikomanager entscheidet final und
platziert Orders **ausschließlich im Alpaca-Paper-Konto** (Spielgeld). Der Code verweigert jeden Start,
wenn `ALPACA_BASE_URL` nicht auf `paper-api.alpaca.markets` zeigt.

Keine Zusatzpakete nötig: nur Python 3.10+ (Standardbibliothek).

## Die Rollen

| Rolle | Datenquelle | Worauf sie achtet |
|---|---|---|
| Insider-Beobachter | Finnhub Insider-Transaktionen, SEC EDGAR (Form 4, 8-K, 13D/G) | Insider-Käufe (stark positiv), große Verkaufswellen, Beteiligungsmeldungen |
| Analyst | Alpaca News, Finnhub Firmen-News + Analystenkonsens + KGV, FMP Profil | Ton der Nachrichten, Analysten-Empfehlungen, Bewertung, anstehende Quartalszahlen |
| Politik-Analyst | Finnhub Wirtschafts-News (+ Politiker-Trades, falls Premium-Zugang) | Zölle, Sanktionen, Fed, Regulierung, Kongress im Zusammenhang mit dem Unternehmen; Makro-Stimmung |
| Quant | Alpaca Kurse (Tag + 15 Min, IEX-Feed) | Trend (SMA20/50), RSI, VWAP, Momentum, Volumen-Ausbrüche |
| Risikomanager | Konto, Positionen, Börsenuhr | Entscheidet final, hat Veto (siehe unten) |

Jede Rolle gibt einen Score von −2 bis +2 mit Begründung. **Kauf** nur, wenn die Summe ≥ 3 ist und
mindestens 2 Rollen positiv sind. **Verkauf** einer gehaltenen Position bei Summe ≤ −2.

**Harte Regeln des Risikomanagers (Veto):** max. 10 % des Kapitals je Position, max. 5 Positionen,
jede Order mit Stop-Loss −4 % und Gewinnziel +8 % (Bracket-Order), keine Käufe bei Tagesverlust ab −3 %,
keine Käufe vor Quartalszahlen, keine Käufe gegen ein negatives Quant-Signal, keine neuen Positionen in den
letzten 45 Minuten, **alles glattstellen 15 Minuten vor Börsenschluss** (Daytrading), keine Leerverkäufe,
keine Penny-Stocks (< 5 $). Alle Werte lassen sich in `settings.json` ändern (Vorlage: `settings.example.json`).

## Keys

Liegen in `keys.env.txt` (oder `.env`) im Projektordner, beide stehen in `.gitignore`.
Pflicht: `ALPACA_API_KEY`, `ALPACA_API_SECRET`, `ALPACA_BASE_URL` (mit oder ohne `/v2`).
Empfohlen: `FINNHUB_API_KEY`, `FMP_API_KEY`, `SEC_EDGAR_USER_AGENT`. Fehlende Quellen werden übersprungen.

## Befehle

```bash
python3 -m tradingbot status                 # Konto, Positionen, offene Orders
python3 -m tradingbot run                    # Entscheidungslauf als Trockenlauf (keine Order)
python3 -m tradingbot run --execute          # Entscheidungslauf + Paper-Orders
python3 -m tradingbot apply decisions/x.json --execute   # Entscheidungen von Claude übernehmen
python3 -m tradingbot flatten --execute      # alle Positionen schließen
```

Jeder Lauf schreibt `reports/<Zeit>_<modus>.md` (lesbarer Bericht) und `.json` (vollständiges Briefing),
dazu `reports/latest.md` / `latest.json`, `logs/runs.log` und `logs/trades.csv`.
Orders werden nur bei geöffneter Börse gesendet (15:30–22:00 MEZ).

## Zwei Betriebsarten

**1. Regelbasiert (vollautomatisch):** `run --execute`. Kostet nichts, ist reproduzierbar, aber die
Textauswertung ist einfach (Schlüsselwörter).

**2. Claude als Entscheider (empfohlen für die geplante Aufgabe im Projekt):** Ohne bezahlten API-Key,
weil Claude in der geplanten Aufgabe selbst urteilt:

1. `python3 -m tradingbot run` erzeugt das Briefing (`reports/latest.md` + `latest.json`).
2. Claude liest es, wägt die fünf Rollen ab und schreibt `decisions/<Zeit>.json`:
   ```json
   {"decisions": [
     {"symbol": "AMZN", "action": "BUY", "conviction": 3.5, "size": 0.8, "reason": "..."},
     {"symbol": "NVDA", "action": "SELL", "reason": "..."}
   ]}
   ```
   `action`: BUY, SELL oder HOLD. `size` (0.1–1) skaliert die Positionsgröße innerhalb des 10-%-Limits.
3. `python3 -m tradingbot apply decisions/<Zeit>.json --execute` – der Risikomanager prüft die harten
   Regeln trotzdem und kann jede Entscheidung per Veto stoppen.

Vorschlag für den Prompt der stündlichen Aufgabe (Mo–Fr, 15:45–21:45 MEZ):

> Im Ordner „Daytrading Sim": Führe `python3 -m tradingbot run` aus und lies `reports/latest.md`
> (Details in `latest.json`). Bewerte als Gruppe aus Insider-Beobachter, Analyst, Politik-Analyst und
> Quant die Kandidaten, achte auf echte Neuigkeiten und Muster. Schreibe deine Entscheidungen nach
> `decisions/<Datum_Uhrzeit>.json` und führe `python3 -m tradingbot apply <Datei> --execute` aus.
> Fasse danach in 5 Sätzen zusammen, was gekauft/verkauft wurde und warum.

## Bekannte Grenzen

- Politiker-Trades (Kongress/Senat) sind bei Finnhub und FMP nur im Bezahlplan verfügbar; der
  Politik-Analyst nutzt deshalb vorerst Nachrichten. Der Code versucht die Endpunkte und überspringt sie ohne Zugriff.
- Der kostenlose Alpaca-Kursfeed (IEX) zeigt nur einen Teil des Handelsvolumens.
- Simulation zu Lern- und Testzwecken, keine Anlageberatung.
