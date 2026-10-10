"""Delivery % per stock per day, from NSE's full bhavcopy (sec_bhavdata_full): one free CSV per trading day for every NSE stock.

Delivery % = shares actually delivered (taken home) / shares traded. High delivery on a rising price means holders, not day traders, are
buying; a price move on thin delivery is mostly speculation. IndianAPI's /historical_data shows the same figure per stock (rounded) but costs one
call each; this file has the whole market in one request. Idempotent: a day replaces itself. Series other than EQ/BE/SM/ST are skipped."""
import csv
import io
import time
from datetime import date, timedelta

import requests

URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{dmy}.csv"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"}
SERIES = {"EQ", "BE", "BZ", "SM", "ST"}


def fetch_day(d: date):
    """Rows [(symbol, iso_date, deliv_qty, deliv_pct)] for one day, or None on a holiday / not yet published."""
    try:
        r = requests.get(URL.format(dmy=d.strftime("%d%m%Y")), headers=UA, timeout=40)
    except requests.RequestException:
        return None
    if r.status_code != 200 or len(r.content) < 10_000:
        return None
    out = []
    for row in csv.DictReader(io.StringIO(r.content.decode("utf-8", "ignore"), newline=""), skipinitialspace=True):
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        if row.get("SERIES") not in SERIES:
            continue
        try:
            qty, pct = int(float(row["DELIV_QTY"])), float(row["DELIV_PER"])
        except (KeyError, ValueError):
            continue  # '-' on days with no delivery data
        out.append((f"NSE:{row['SYMBOL']}", d.isoformat(), qty, pct))
    return out


def ingest_days(conn, days, known: set[str], pause: float = 0.4) -> int:
    n = 0
    for d in days:
        rows = fetch_day(d)
        time.sleep(pause)
        if not rows:
            continue
        conn.executemany("INSERT OR REPLACE INTO delivery_data (symbol, date, deliv_qty, deliv_pct) VALUES (?,?,?,?)", [r for r in rows if r[0] in known])
        conn.commit()
        n += len(rows)
    return n


def recent_days(conn, back: int = 14):
    """Weekdays of the last `back` days that are not stored yet (a stored day is never re-fetched)."""
    have = {r[0] for r in conn.execute("SELECT DISTINCT date FROM delivery_data WHERE date >= ?", ((date.today() - timedelta(days=back)).isoformat(),))}
    today = date.today()
    return [d for d in (today - timedelta(days=i) for i in range(back, -1, -1)) if d.weekday() < 5 and d.isoformat() not in have]
