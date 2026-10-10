"""Recent exchange filings for one stock (results, board outcomes, dividends, order wins, pledge changes...).

The list itself is BSE's own announcements feed: free, primary, exact time, category and a link to the filing PDF. IndianAPI's
/recent_announcements adds a one-line plain-English summary per filing; it costs a call per stock, so it is fetched at most once per 12 hours per
stock, only while the monthly cap (ia_usage) has room, and joined to the BSE rows by the PDF's id. Without the key or the budget the list
simply has no summary line. Cached per stock for 3 hours and shared by every user."""
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone

import requests

from app.db import get_market_conn
from app.ingestion import bse_source as bse
from app.ingestion.results_refresh_source import IA_HOST, MONTHLY_CAP, _bump, _used

logger = logging.getLogger(__name__)
FRESH = timedelta(hours=3)
IA_FRESH = timedelta(hours=12)
WINDOW_DAYS = 45
_scrips: dict = {"at": 0.0, "map": {}}
_SUMMARY = re.compile(r"\s(\d{1,2} [A-Z][a-z]{2}) - (.+)$")


def _now():
    return datetime.now(timezone.utc)


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse(ts: str | None):
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if ts else None


def scrip_code(symbol: str):
    if time.time() - _scrips["at"] > 86400 or not _scrips["map"]:
        _scrips["map"] = {f"NSE:{r['scrip_id']}": r["SCRIP_CD"] for r in bse.BseSource()._scrip_rows() if r.get("scrip_id") and r.get("SCRIP_CD")}
        _scrips["at"] = time.time()
    return _scrips["map"].get(symbol)


def _bse_items(code) -> list[dict]:
    today = date.today()
    params = {"pageno": 1, "strCat": "-1", "subcategory": "-1", "strPrevDate": (today - timedelta(days=WINDOW_DAYS)).strftime("%Y%m%d"),
              "strToDate": today.strftime("%Y%m%d"), "strSearch": "P", "strscrip": str(code), "strType": "C"}
    r = requests.get(f"{bse.API_BASE}/AnnSubCategoryGetData/w", params=params, headers=bse.HEADERS, timeout=30)
    r.raise_for_status()
    out = []
    for x in (r.json() or {}).get("Table") or []:
        att = (x.get("ATTACHMENTNAME") or "").strip()
        out.append({
            "id": att.rsplit(".", 1)[0].lower() if att else None,
            "title": " ".join((x.get("NEWSSUB") or x.get("HEADLINE") or "").split()),
            "at": (x.get("DissemDT") or x.get("NEWS_DT") or "")[:16].replace("T", " "),
            "category": (x.get("CATEGORYNAME") or "").strip() or None,
            "link": f"{bse.SITE_BASE}/xml-data/corpfiling/AttachLive/{att}" if att else None,
            "summary": None,
        })
    return out


def _ia_summaries(ticker: str) -> dict:
    """{pdf id: one-line summary} from IndianAPI, or {} when unavailable."""
    key = os.environ.get("INDIANAPI_KEY", "").strip()
    if not key:
        return {}
    with get_market_conn() as m:
        if _used(m) >= MONTHLY_CAP:
            return {}
        _bump(m)
    r = requests.get(f"{IA_HOST}/recent_announcements", params={"stock_name": ticker}, headers={"X-Api-Key": key}, timeout=40)
    if r.status_code != 200:
        return {}
    out = {}
    for x in r.json() if isinstance(r.json(), list) else []:
        m_id = re.search(r"Pname=([0-9a-fA-F-]{36})", x.get("link") or "")
        s = _SUMMARY.search(x.get("title") or "")
        if m_id and s:
            out[m_id.group(1).lower()] = s.group(2).strip()
    return out


def _ia_only_items(ticker: str) -> list[dict]:
    """The whole list from IndianAPI, for hosts BSE refuses (its API answers 403 to the VM's datacenter address). Dates carry no year or time."""
    key = os.environ.get("INDIANAPI_KEY", "").strip()
    if not key:
        return []
    with get_market_conn() as m:
        if _used(m) >= MONTHLY_CAP:
            return []
        _bump(m)
    r = requests.get(f"{IA_HOST}/recent_announcements", params={"stock_name": ticker}, headers={"X-Api-Key": key}, timeout=40)
    data = r.json() if r.status_code == 200 else []
    out, today = [], date.today()
    for x in data if isinstance(data, list) else []:
        t = " ".join((x.get("title") or "").split())
        s = _SUMMARY.search(t)
        when = None
        if s:
            try:
                d = datetime.strptime(f"{s.group(1)} {today.year}", "%d %b %Y").date()
                when = (d if d <= today else d.replace(year=d.year - 1)).isoformat()
            except ValueError:
                pass
        m_id = re.search(r"Pname=([0-9a-fA-F-]{36})", x.get("link") or "")
        out.append({"id": m_id.group(1).lower() if m_id else None, "title": t[:s.start()] if s else t, "at": when or "", "category": None,
                    "link": x.get("link"), "summary": s.group(2).strip() if s else None})
    return out


def get_announcements(symbol: str, limit: int = 15) -> dict:
    """{"items": [...], "as_of": utc string, "error": str|None}. Never raises: a failure returns the stale cache or an empty list."""
    with get_market_conn() as m:
        row = m.execute("SELECT * FROM announcements_cache WHERE symbol = ?", (symbol,)).fetchone()
    items = json.loads(row["items_json"]) if row else []
    fetched, ia_at = _parse(row["fetched_at"]) if row else None, _parse(row["ia_at"]) if row else None
    error = None
    if not row or _now() - fetched > FRESH:
        try:
            try:
                code = scrip_code(symbol)
                bse_ok = True
            except requests.RequestException:  # BSE refuses some hosts outright (403): fall back to IndianAPI's own list
                code, bse_ok = None, False
                ia_items = _ia_only_items(symbol.split(":")[-1])
                if ia_items:
                    items, fetched, ia_at = ia_items, _now(), _now()
                    with get_market_conn() as m:
                        m.execute("INSERT INTO announcements_cache (symbol, fetched_at, ia_at, items_json) VALUES (?,?,?,?) "
                                  "ON CONFLICT(symbol) DO UPDATE SET fetched_at=excluded.fetched_at, ia_at=excluded.ia_at, items_json=excluded.items_json",
                                  (symbol, _stamp(fetched), _stamp(ia_at), json.dumps(items)))
                        m.commit()
                else:
                    error = "Filings are unavailable on this server right now."
            if code:
                old = {i["id"]: i.get("summary") for i in items if i.get("id")}
                items = _bse_items(code)
                for i in items:
                    i["summary"] = old.get(i["id"])
                fetched = _now()
                if not ia_at or _now() - ia_at > IA_FRESH:
                    try:
                        sums = _ia_summaries(symbol.split(":")[-1])
                        for i in items:
                            i["summary"] = sums.get(i["id"]) or i["summary"]
                        ia_at = _now() if sums else ia_at
                    except Exception as e:  # noqa: BLE001 - summaries are a bonus
                        logger.info("announcement summaries skipped for %s: %s", symbol, e)
                with get_market_conn() as m:
                    m.execute("INSERT INTO announcements_cache (symbol, fetched_at, ia_at, items_json) VALUES (?,?,?,?) "
                              "ON CONFLICT(symbol) DO UPDATE SET fetched_at=excluded.fetched_at, ia_at=excluded.ia_at, items_json=excluded.items_json",
                              (symbol, _stamp(fetched), _stamp(ia_at) if ia_at else None, json.dumps(items)))
                    m.commit()
            elif bse_ok and not items:
                error = "This stock has no BSE filings feed."
        except Exception as e:  # noqa: BLE001
            logger.warning("announcements fetch failed for %s: %s", symbol, e)
            error = "The exchange feed did not answer; showing what we last had." if items else "The exchange feed did not answer. Try again in a minute."
    return {"items": items[:limit], "as_of": _stamp(fetched) if fetched else None, "error": error}
