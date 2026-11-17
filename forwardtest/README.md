# Forward-Test: Day Trader Skill vs. CIM

Paper-Forward-Test der Day-Trader-Strategie (Original-Regelwerk von
Jakeschincariol/day-trader-skill), parallel zu "Claude Investment Management".
Kein Broker, keine echten Orders.

## Wie es läuft

Eine geplante Aufgabe läuft Mo–Fr um 22:30 MEZ (nach US-Börsenschluss) und
führt `python3 forwardtest/daily_run.py` aus. Das Skript spielt den Handelstag
regelgetreu nach. Es nutzt dabei nur Daten, die zum jeweiligen Zeitpunkt
bekannt waren, und schreibt ins Journal. Danach wird das Journal committet und
das Dashboard aktualisiert.

| Schritt | Regel |
|---|---|
| Watchlist | Tages-Setups des Scanners auf Schlusskursen bis D-1. Vortag: Kurs ≥ $5, Volumen ≥ 1 Mio. Die 30 Titel mit dem höchsten Dollar-Volumen |
| Einstieg | ORB: Range 9:30–9:45 ET, erster 5-Min-Schlusskurs über dem Range-Hoch, Einstieg 9:45–15:30 |
| Ausstieg | Stop an der Range-Mitte (abgerundet), Ziel 2R, glattstellen 15:55, keine Übernacht-Position |
| Größe | min(100 $ / Risiko je Aktie, 75 % Kapital, 8.000 $ Order, freies Cash nach 10 % Puffer) |
| Limits | max. 3 Einstiege pro Tag, max. Tagesverlust 300 $, CRV ≥ 1 |
| Kosten | 0,05 % Slippage auf Market-Fills (Einstieg, Stop, Glattstellung), Kommission 0,005 $/Aktie, mind. 1 $ je Seite |
| Simulation | Stop vor Ziel im selben Bar (konservativ), Gap durch den Stop füllt zum Open |

**Variante A** ist das Original. **Variante B** ist A plus zwei Filter:
Trigger-Volumen ≥ 1,5× Range-Schnitt und SPY über seinem Tagesopen.
Beide Varianten laufen mit eigenem Kapital von 10.000 $, Sizing auf fester Basis.

Das Regelwerk in `config.json` ist ab Start eingefroren. Neue Ideen kommen als
weitere Variante dazu, nicht als Änderung an A oder B.

## Dateien

- `journal/trades.csv`: jeder simulierte Trade, inkl. Time-Machine-Prognose
- `journal/days.csv`: je Tag und Variante Watchlist, Trigger, Trades, Netto
- `logs/<Datum>.json`: Watchlist, alle Trigger, übersprungene Trigger mit Grund

## Bekannte Grenzen

- Die Fills sind simuliert. Echte Slippage bei Breakouts ist oft schlechter.
- Yahoo-Daten sind inoffiziell. Fehlende 5-Min-Daten stehen im Log und in `days.csv`.
- Der Vorfilter auf das Universum (Volumen ≥ 300k) nutzt das aktuelle Volumen.
  Für den 4-Wochen-Backfill ist das ein kleiner Look-ahead. Die eigentliche
  Liquiditätsprüfung erfolgt point-in-time am Vortag.
- Die Watchlist bevorzugt große Liquide Titel (Dollar-Volumen). Das ist eine
  eigene mechanische Auswahlregel. Im Original wählt der Mensch.

## Entscheidungsregel

Frühestens nach 60 Trades bzw. ca. 3 Monaten. Weiterverfolgen nur, wenn der
Erwartungswert nach Kosten über +0,2R liegt und der Drawdown kleiner ist als bei CIM.
