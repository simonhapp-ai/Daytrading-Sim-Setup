# Paper-Trading-Agentengruppe (US-Aktien)

Fünf Rollen bewerten stündlich eine Auswahl von US-Aktien, ein Risikomanager entscheidet final und
platziert Orders **ausschließlich im Alpaca-Paper-Konto** (Spielgeld). Der Code verweigert jeden Start,
wenn `ALPACA_BASE_URL` nicht auf `paper-api.alpaca.markets` zeigt.

Keine Zusatzpakete nötig: nur Python 3.10+ (Standardbibliothek).

## Handelszeiten

Alpaca handelt nur US-Aktien, deshalb gelten US-Zeiten. Der Bot nutzt alle drei Sitzungen
(genaue Zeiten inkl. Feiertagen kommen aus dem Alpaca-Kalender; in den Wochen der Zeitumstellung verschiebt sich alles um 1 Std.):

| Sitzung | Deutsche Zeit | Orders | Größe |
|---|---|---|---|
| Vorbörse | 10:00–15:30 | nur Limit-Orders (`extended_hours`), Limit 0,2 % über Brief | halbe Größe, Kaufschwelle +0,5 |
| Hauptbörse | 15:30–22:00 | Market-Order mit Stop-Loss/Gewinnziel (Bracket) | normal |
| Nachbörse | 22:00–02:00 | nur Limit-Orders, Limit 0,2 % über Brief / unter Geld | halbe Größe, Kaufschwelle +0,5 |

Außerhalb der Hauptbörse gibt es weniger Umsatz und breitere Spannen. Deshalb: kein Kauf bei einer
Geld-/Brief-Spanne über 0,3 %, und weil Alpaca dort keine Bracket-Orders erlaubt, prüft der Bot bei jedem Lauf
selbst Stop-Loss und Gewinnziel und verkauft per Limit-Order. Kurse für Vor-/Nachbörse kommen gratis nur
mit ~15 Min. Verzögerung; das enge Limit sorgt dafür, dass bei veraltetem Kurs einfach nichts ausgeführt wird.
Glattgestellt wird 15 Min. vor Ende der Hauptbörse und vor Ende der Nachbörse. Abschalten: `"extended_hours": false` in `settings.json`.

## Die Rollen

| Rolle | Datenquelle | Worauf sie achtet |
|---|---|---|
| Insider-Beobachter | Finnhub Insider-Transaktionen, SEC EDGAR (Form 4, 8-K, 13D/G) | Insider-Käufe (stark positiv), große Verkaufswellen, Beteiligungsmeldungen |
| Analyst | Alpaca News, Finnhub Firmen-News + Analystenkonsens + KGV, FMP Profil | Ton der Nachrichten, Analysten-Empfehlungen, Bewertung, anstehende Quartalszahlen |
| Politik-Analyst | Finnhub Wirtschafts-News (+ Politiker-Trades, falls Premium-Zugang) | Zölle, Sanktionen, Fed, Regulierung, Kongress im Zusammenhang mit dem Unternehmen; Makro-Stimmung |
| Quant | Alpaca Kurse (Tag + 15 Min, Gesamtmarkt, 15 Min. verzögert) | Trend (SMA20/50), RSI, VWAP, Momentum, Volumen-Ausbrüche |
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
dazu `reports/latest.md` / `latest.json`, `logs/runs.log` und `logs/trades.csv`. Danach exportiert
jeder Lauf automatisch `dashboard/portfolio.json` und `dashboard/chat.json` für die Dashboard-Artefakte
(ohne Keys; einzeln: `python3 -m tradingbot.dashboard`).
Orders werden nur in einer erlaubten Sitzung gesendet (siehe Handelszeiten).

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
   Optional für den Agenten-Chat im Dashboard: `"discussion": [{"role": "Quant", "symbol": "AMZN", "text": "..."}]`
   (Rollen widersprechen oder bestätigen sich gegenseitig) und `"summary": "..."` (Fazit der Runde in 1–3 Sätzen).
3. `python3 -m tradingbot apply decisions/<Zeit>.json --execute` – der Risikomanager prüft die harten
   Regeln trotzdem und kann jede Entscheidung per Veto stoppen.

Vorschlag für den Prompt der stündlichen Aufgabe (Mo–Fr, stündlich 10:15–01:15 MEZ, mit Vor- und Nachbörse):

> Im Ordner „Daytrading Sim": Führe `python3 -m tradingbot run` aus und lies `reports/latest.md`
> (Details in `latest.json`). Bewerte als Gruppe aus Insider-Beobachter, Analyst, Politik-Analyst und
> Quant die Kandidaten, achte auf echte Neuigkeiten und Muster. Schreibe deine Entscheidungen nach
> `decisions/<Datum_Uhrzeit>.json`, mit einem Feld `discussion` (kurze Wortmeldungen der Rollen zu den
> wichtigsten Aktien, gern mit Widerspruch) und `summary` (Fazit der Runde), und führe
> `python3 -m tradingbot apply <Datei> --execute` aus.
> Fasse danach in 5 Sätzen zusammen, was gekauft/verkauft wurde und warum.

## Bekannte Grenzen

- Politiker-Trades (Kongress/Senat) sind bei Finnhub und FMP nur im Bezahlplan verfügbar; der
  Politik-Analyst nutzt deshalb vorerst Nachrichten. Der Code versucht die Endpunkte und überspringt sie ohne Zugriff.
- Kurse sind im Gratis-Plan 15 Minuten verzögert (Gesamtmarkt). Für stündliche Entscheidungen reicht das.
- Finnhub erlaubt 60 Aufrufe pro Minute; der Bot drosselt sich selbst, ein Lauf dauert deshalb etwa 2 Minuten.
- Simulation zu Lern- und Testzwecken, keine Anlageberatung.
