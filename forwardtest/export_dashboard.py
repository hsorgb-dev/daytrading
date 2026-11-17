#!/usr/bin/env python3
"""Journal -> JSON-Dateien fuers Dashboard (typisierte Zahlen, Herkunft in meta).

    python3 forwardtest/export_dashboard.py      # -> forwardtest/dashboard/trades.json, days.json
"""
import csv
import datetime
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
NUM = {"dollar_vol_prev", "or_high", "or_low", "volume_x", "entry", "stop", "target", "risk_ps", "shares", "notional",
       "exit_price", "gross_pnl", "costs", "net_pnl", "r_net", "tm_target_pct", "tm_stop_pct", "tm_neither_pct",
       "tm_avg_r", "tm_hist_avg_r", "watchlist", "with_data", "triggers", "taken", "skipped", "spy_open",
       "spy_close"}


def load(name):
    with open(os.path.join(HERE, "journal", name), newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        for k, v in r.items():
            if k in NUM:
                r[k] = None if v in ("", None) else float(v)
                if r[k] is not None and k in ("shares", "watchlist", "with_data", "triggers", "taken", "skipped"):
                    r[k] = int(r[k])
            elif v == "":
                r[k] = None
    return rows


def main():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    meta = [{"label": "Erzeugt von", "value": "forwardtest/daily_run.py, Repo hsorgb-dev/daytrading"},
            {"label": "Exportiert", "value": stamp},
            {"label": "Regelwerk (config.json)", "value": json.dumps(cfg, ensure_ascii=False, indent=1),
             "type": "code", "lang": "json"}]
    out = os.path.join(HERE, "dashboard")
    os.makedirs(out, exist_ok=True)
    for name in ("trades", "days"):
        rows = load(name + ".csv")
        with open(os.path.join(out, name + ".json"), "w", encoding="utf-8") as fh:
            json.dump({"rows": rows, "meta": meta}, fh, ensure_ascii=False)
        print("%s.json: %d Zeilen" % (name, len(rows)))


if __name__ == "__main__":
    main()
