"""Recent news for one symbol, feeding the daily Insights narrative. Modeled directly on
app/ingestion/finnhub_source.py's existing request/key/rate-limit pattern (same env var,
same NotConfiguredError, same India-ADR-ticker workaround — Finnhub's free tier 403s on
an NSE-suffixed symbol queried directly, confirmed working via the company's US ADR
ticker instead, re-using that file's verified INDIA_ADR_TICKERS map rather than
re-declaring it).

Live-verified response shape before writing this (not assumed): `company-news` returns
a list of {category, datetime (unix epoch seconds, NOT an ISO string), headline, id,
image, related, source, summary, url}."""

import os
import time
from datetime import date, datetime, timedelta

import requests

from app.ingestion.base import NotConfiguredError
from app.ingestion.finnhub_source import INDIA_ADR_TICKERS

API_KEY = os.environ.get("FINNHUB_API_KEY")
BASE_URL = "https://finnhub.io/api/v1/company-news"
RATE_LIMIT_SECONDS = 1.1  # same free-tier 60 calls/minute cap as finnhub_source.py
MAX_ITEMS = 10


def get_recent_news(symbol: str, days: int = 7) -> list[dict] | dict:
    """Returns the most recent news items for `symbol`, or a {"skipped": ...} marker if
    there's no way to query this symbol on Finnhub's free tier (an India symbol with no
    verified US ADR mapping) — never guesses a ticker, matches finnhub_source.py's own
    caution on this exact point."""
    if not API_KEY:
        raise NotConfiguredError("FINNHUB_API_KEY not set — sign up free at finnhub.io, no KYC needed.")

    if symbol.startswith("NSE:"):
        ticker = INDIA_ADR_TICKERS.get(symbol)
        if not ticker:
            return {"skipped": f"no verified US ADR ticker mapped for {symbol}"}
    else:
        ticker = symbol.split(":")[1]

    time.sleep(RATE_LIMIT_SECONDS)
    try:
        resp = requests.get(
            BASE_URL,
            params={
                "symbol": ticker,
                "from": (date.today() - timedelta(days=days)).isoformat(),
                "to": date.today().isoformat(),
                "token": API_KEY,
            },
            timeout=20,
        )
        resp.raise_for_status()
        items = resp.json()
    except requests.RequestException as e:
        return {"skipped": f"Finnhub request failed: {e}"}

    items = sorted(items, key=lambda i: i.get("datetime", 0), reverse=True)[:MAX_ITEMS]
    return [
        {
            "headline": i.get("headline"),
            "summary": i.get("summary"),
            "url": i.get("url"),
            "source": i.get("source"),
            "datetime": datetime.fromtimestamp(i["datetime"]).isoformat() if i.get("datetime") else None,
        }
        for i in items
    ]


# ----------------------------------------------------------------------------------------------
# Company-name news for ANY market. Finnhub's free tier only covers a handful of Indian ADRs, so Indian stocks
# usually came back "skipped" and the daily analysis had no news at all. Google News' RSS search works for every
# company and needs no key; Finnhub is added on top for US symbols.
# ----------------------------------------------------------------------------------------------

import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime

_UA = {"User-Agent": "Mozilla/5.0 (compatible; StorylineBot/1.0)"}


def _clean_title(t: str) -> str:
    return re.sub(r"\s+-\s+[^-]{2,40}$", "", t or "").strip()  # Google News appends " - Publisher"


def google_news(query: str, days: int = 2, limit: int = 8) -> list[dict]:
    """Recent headlines for a search query: [{title, source, url, when(datetime, UTC)}], newest first."""
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(f"{query} when:{days}d")
           + "&hl=en-IN&gl=IN&ceid=IN:en")
    r = requests.get(url, timeout=20, headers=_UA)
    r.raise_for_status()
    if b"<!ENTITY" in r.content or b"<!DOCTYPE" in r.content:  # a news feed never needs these; refuse rather than parse
        raise ValueError("feed contains DTD/entity declarations")
    items = []
    for it in ET.fromstring(r.content).findall(".//item")[: limit * 2]:
        try:
            when = parsedate_to_datetime(it.findtext("pubDate"))
        except Exception:  # noqa: BLE001
            continue
        src = it.find("source")
        items.append({"title": _clean_title(it.findtext("title")), "source": (src.text if src is not None else "") or "",
                      "url": it.findtext("link"), "when": when})
    items.sort(key=lambda x: x["when"], reverse=True)
    return items[:limit]


def _search_name(company_name: str) -> str:
    name = re.sub(r"\b(limited|ltd|inc|incorporated|corp|corporation|plc|co)\b\.?", "", company_name or "", flags=re.I)
    return re.sub(r"\s+", " ", name).strip().title()


def company_news(symbol: str, company_name: str, days: int = 7, limit: int = 8) -> list[dict]:
    """Numbered-ready headlines about one company over the last `days`: Google News by company name (all markets) plus
    Finnhub for US tickers. Never raises — a news outage must not stop the analysis; returns whatever it could get."""
    out: list[dict] = []
    seen: set[str] = set()

    def add(title, source, url, when):
        key = re.sub(r"\W+", " ", (title or "").lower())[:80]
        if title and key not in seen:
            seen.add(key)
            out.append({"title": title, "source": source, "url": url, "when": when})

    try:
        for it in google_news(f'"{_search_name(company_name)}" stock', days=days, limit=limit):
            add(it["title"], it["source"], it["url"], it["when"])
    except Exception:  # noqa: BLE001
        pass
    if not symbol.startswith("NSE:") and API_KEY:
        try:
            for n in get_recent_news(symbol, days=days) or []:
                if isinstance(n, dict) and n.get("headline"):
                    when = datetime.fromisoformat(n["datetime"]).replace(tzinfo=timezone.utc) if n.get("datetime") else None
                    add(n["headline"], n.get("source", ""), n.get("url"), when)
        except Exception:  # noqa: BLE001
            pass
    out.sort(key=lambda x: x["when"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out[:limit]
