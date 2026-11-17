# Forward-Test: Day Trader Skill vs. CIM

Paper-Forward-Test der Day-Trader-Strategie (Original-Regelwerk von
Jakeschincariol/day-trader-skill), parallel zu "Claude Investment Management".
Kein Broker, keine echten Orders.

## Wie es läuft

Eine geplante Aufgabe läuft Mo–Fr um 22:40 MEZ (nach US-Börsenschluss) und
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

Seit 17.11.2026 laufen drei weitere klassische Intraday-Strategien mit. Sie nutzen
dieselbe Watchlist, Größe, Limits, Kosten und Simulation wie A, sind nur Long und
bringen ihren eigenen Stop mit:

| Variante | Strategie | Einstieg | Stop | Ziel |
|---|---|---|---|---|
| C | VWAP-Pullback | 10:00–15:00 ET: Bar taucht an den VWAP (≤ +0,1 %), schließt grün darüber; Vor-Bar über VWAP, Tageshoch ≥ 0,5 % über VWAP, Kurs über Tagesopen | min(Bar-Tief, VWAP) − 0,05 % | 2R |
| D | Mean Reversion | 5-Min-Schluss unter Bollinger 20/2,5σ und RSI(2) < 5, dann innerhalb von 3 Bars Schluss über dem Setup-Hoch (Indikatoren laufen über den Vortag) | tiefstes Tief seit Setup − 0,05 % | SMA20 (Trade entfällt bei CRV < 1) |
| E | Vortages-Hoch/-Tief | PDH-Ausbruch: erster Schluss über PDH mit Volumen ≥ 1,5× der letzten 20 Bars (Open unter PDH). Oder PDL-Sweep: Tief unter PDL, innerhalb von 6 Bars Schluss zurück darüber (Open über PDL) | Bar-Tief bzw. Sweep-Tief − 0,05 % | 2R |

Signale, die vor 9:45 (bzw. 10:00) schon bestätigt waren, verfallen. Der Backfill von
C–E für 20.10.–16.11. lief mit `--variants C,D,E` und nutzt die in `logs/` festgehaltenen
Watchlists von A/B; A und B blieben dabei unverändert.

**Variante C2** (ab 17.11.2026, ohne Backfill) nimmt dieselben Einstiege wie C, setzt den
Stop 1,5× so weit vom Einstieg (gleiches $-Risiko, also weniger Stück) und hat kein festes
Ziel: Sie hält bis zum Stop oder bis 15:55. Sie ist aus der Auswertung der ersten 20 Tage
abgeleitet (bei C griffen 43 % der Stops im ersten Bar, Gewinner liefen oft über 3R).
Deshalb läuft sie nur vorwärts (`forward_from` in `config.json`). Die Time Machine braucht
ein Ziel und läuft für C2 nicht.

**Variante C3** (ab 17.11.2026, ohne Backfill) ist C2 plus Trailing-Stop: Sobald der
Trade +1R erreicht, folgt der Stop dem bisherigen Hoch im Abstand 1,5R (nur nach oben,
wirksam ab dem nächsten Bar). Ausstiegsgrund im Journal: `trail` bzw. `trail_gap`.

**Varianten F und G** (seit 17.11.2026, Backfill ab 20.10.) setzen zwei veröffentlichte
Studien regelgetreu um. Sie handeln long und short und haben eigene Größenregeln:

- **F, Stocks-in-Play-ORB** (Zarattini/Barbon/Aziz 2024): Universum Open > 5 $, Ø-Volumen
  14 Tage ≥ 1 Mio., ATR(14) > 0,50 $. Die 20 Titel mit dem höchsten relativen Volumen der
  ersten 5 Minuten (vs. Schnitt der 14 Vortage, mind. 100 %). Grüne erste Kerze: nur long per
  Stop-Order am Hoch; rot: nur short am Tief; Doji: kein Trade. Stop 10 % ATR, Ausstieg 16:00.
  1 % Risiko je Trade, Gesamtposition bis 4× Kapital, Mindestorder 500 $. Einstieg und Stop
  laufen auf 1-Minuten-Bars; im Einstiegs-Bar gilt ein Pfadmodell (grüner Bar: erst Tief, dann
  Hoch; roter Bar umgekehrt).
- **G, SPY Intraday-Momentum** (Zarattini/Aziz/Barbon 2024): Noise Area aus der mittleren
  absoluten Bewegung seit dem Open je Uhrzeit (14 Tage), Band um max/min(Open, Vortagesschluss).
  Prüfung nur 10:00, 10:30, … 15:30: über dem Band long, darunter short. Trailing-Stop
  max(Band, VWAP) bzw. min(Band, VWAP), zu denselben Zeiten geprüft, Umkehr möglich. Ausstieg
  16:00. Größe Kapital × min(4, 2 % ÷ Tagesvolatilität 14 Tage). Slippage 0,01 %.

**G2** ist G mit 1,5-fach breiter Noise Area (VM 1,5, laut Studie höhere Sharpe Ratio),
**GQ** sind die Regeln von G unverändert auf QQQ. Beide mit Backfill ab 20.10.

**Varianten H, I, J** (seit 17.11.2026, Backfill ab 20.10.):

- **H, Market Intraday Momentum** (Gao/Han/Li/Zhou 2018): Rendite vom Vortagesschluss bis
  10:00 bestimmt die Richtung eines SPY-Trades von 15:30 bis 16:00. Kein Stop, Größe wie G.
  R misst das Ergebnis in typischen 30-Minuten-Bewegungen (Tagesvola ÷ √13).
- **I, 5-Min-ORB auf QQQ** (Zarattini/Aziz 2023): grüne erste 5-Min-Kerze long, rote short,
  Einstieg zum Open des zweiten Bars, Stop am anderen Ende der ersten Kerze, Ziel 10R, sonst
  16:00. 1 % Risiko, bis 4× Hebel.
- **J, Gap-Continuation** (Earnings-Ersatz): Gap ≥ 4 %, Volumen 9:30–10:00 ≥ 3× des
  14-Tage-Schnitts, um 10:00 in Gap-Richtung bestätigt. Einstieg 10:00, Stop am Extrem der
  ersten 30 Minuten, Ausstieg 16:00. Ein Nachweis über die SEC-Meldung (8-K, Punkt 2.02) ist
  vorbereitet, braucht aber eine SEC-Kontaktangabe im Absender.

F, G, I und J bräuchten in der Praxis ein Margin-Konto (Hebel, Shorts); für Daytrading mit
US-Aktien gilt dort meist die Pattern-Day-Trader-Regel mit 25.000 $ Mindestkapital.

**Gemeinsamer Vorwärts-Start am 17.11.2026, mit Reinvestieren.** Ab diesem Tag startet
jede Variante mit 10.000 $ und rechnet mit ihrem aktuellen Kontostand: 1 % Risiko vom
Kontostand je Trade, und alle $-Grenzen (Tagesverlust, max. Order, Kapital für Cash und
Hebel) wachsen oder schrumpfen mit. Die Mindestorder von 500 $ bleibt fest. Gesteuert
über `reinvest.from` in `config.json`. Die Tage davor (Backfill ab 20.10.) liefen mit
fester Basis von 10.000 $ und zählen nur als Referenz. Das Dashboard zeigt alle
Kennzahlen ab dem Start und den Backfill getrennt.

Das Regelwerk in `config.json` ist ab Start eingefroren (C–E ab 17.11.2026). Neue Ideen kommen als
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

Gezählt ab dem Vorwärts-Start am 17.11.2026. Frühestens nach 60 Trades bzw. ca. 3 Monaten. Weiterverfolgen nur, wenn der
Erwartungswert nach Kosten über +0,2R liegt und der Drawdown kleiner ist als bei CIM.
