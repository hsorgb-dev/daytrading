# Backtest Variante L (Luk-Pullback-Swing)

Stand 2026-11-17 · Yahoo-Tageskerzen · 3272 Titel im Universum (heutige Liste, Survivorship Bias) · erzeugt von forwardtest/backtest_luk.py

| Lauf | Zeitraum | Trades | /Jahr | Treffer | Ø Gewinn | Ø Verlust | Ø R netto | PF | CAGR | MaxDD | Sharpe | Ø investiert | Top-10-Anteil | Haltedauer Gew./Verl. |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| repo | 2016-01-04–2026-08-31 | 256 | 24 | 27 % | +12.6 % | -2.9 % | +0.40 | 1.48 | +4.5 % | -13 % | 0.56 | 6 % | 147 % | 10 / 2 |
| L | 2016-01-04–2026-11-17 | 435 | 40 | 26 % | +12.9 % | -3.0 % | +0.36 | 1.43 | +6.5 % | -23 % | 0.61 | 11 % | 108 % | 10 / 2 |
| L ohne QQQ-Filter | 2016-01-04–2026-11-17 | 552 | 51 | 25 % | +12.5 % | -3.0 % | +0.30 | 1.38 | +7.1 % | -26 % | 0.59 | 14 % | 107 % | 11 / 2 |

Referenz-Repo, Lauf full (CRSP/TAQ, NBBO-Kosten, Margin-Zins): 287 Trades (27/Jahr), 29 % Treffer, Ø Gewinn +14,3 %, Ø Verlust −2,9 %, CAGR 12,1 %, MaxDD −13 %, Sharpe 0,83, Ø investiert 8 %, Top-10 = 111 % des Netto-Gewinns.

## Nach Jahren

| Jahr | repo | Trades | Treffer | MaxDD | L | Trades | Treffer | MaxDD |
|---|---|---|---|---|---|---|---|---|
| 2016 | -5.7 % | 9 | 0 % | -6 % | -0.5 % | 13 | 8 % | -5 % |
| 2017 | -0.2 % | 2 | 50 % | -1 % | -4.9 % | 9 | 22 % | -5 % |
| 2018 | -2.1 % | 4 | 0 % | -2 % | -1.4 % | 8 | 25 % | -2 % |
| 2019 | +2.3 % | 6 | 67 % | -1 % | +0.0 % | 10 | 40 % | -3 % |
| 2020 | +13.3 % | 48 | 29 % | -8 % | +14.1 % | 87 | 25 % | -15 % |
| 2021 | +0.1 % | 36 | 22 % | -9 % | -2.6 % | 61 | 21 % | -20 % |
| 2022 | +0.7 % | 14 | 29 % | -4 % | +3.1 % | 25 | 20 % | -4 % |
| 2023 | +19.5 % | 21 | 43 % | -6 % | +15.7 % | 35 | 31 % | -8 % |
| 2024 | -0.2 % | 21 | 29 % | -8 % | +18.3 % | 42 | 33 % | -10 % |
| 2025 | +23.0 % | 52 | 25 % | -7 % | +25.7 % | 89 | 28 % | -10 % |
| 2026 | +1.0 % | 43 | 23 % | -11 % | +8.3 % | 56 | 23 % | -9 % |

## Referenz wie die anderen Varianten (feste Basis 10.000 $, Positionen am Ende glattgestellt)

| Zeitraum | Trades | Netto $ | Ø R netto | Treffer | PF | MaxDD $ |
|---|---|---|---|---|---|---|
| 23.09.–17.10. | 7 | +48 | +0.15 | 29 % | 1.15 | -314 |
| 20.10.–16.11. | 0 | +0 | +0.00 | 0 % | – | 0 |
| 23.09.–16.11. | 7 | +129 | +0.38 | 29 % | 1.41 | -314 |
| 20.10.–16.11. ohne QQQ-Filter | 4 | -192 | -0.96 | 0 % | 0.00 | -192 |

## Die 5 besten Trades von L

- LEU 2025-05-22→2025-06-13: +1709 $ (+23.6 R, max_hold)
- MARA 2023-12-12→2024-01-02: +1336 $ (+23.2 R, ema9)
- ARQT 2024-02-08→2024-03-01: +1303 $ (+19.4 R, max_hold)
- TXG 2026-09-04→2026-09-28: +1154 $ (+12.4 R, max_hold)
- CRDO 2025-08-25→2025-09-16: +1135 $ (+14.4 R, max_hold)
