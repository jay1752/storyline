"""The daily "what moved the world" briefing, posted into the Community channel.

Design rule: the numbers and the sources are produced by CODE, never by the model.
  1. Live quotes (oil, gas, yields, dollar, rupee, indices, VIX, metals, theme ETFs) come from yfinance and
     are rendered straight into a markdown table.
  2. Fresh headlines (last 48h) per theme come from Google News RSS (+ Finnhub general news) and are numbered.
  3. The model only writes the short narrative per theme from THOSE numbered headlines and quotes, citing [n].
     It is told to say "No major update" rather than fill space, and never to give buy/sell advice.
  4. The "Sources" list is built from the [n] the model actually cited, so every citation resolves to a real link.
If every model in the chain fails (quota, outage), a data-only briefing (table + headlines) is posted instead,
labelled as such — a quiet day for the AI should not mean a silent channel.

One briefing per IST date (community_posts.briefing_date is UNIQUE); `force=True` regenerates today's in place."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from app.ai.providers import platform_chain
from app.db import get_users_conn

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))  # no DST, so a fixed offset is exact (and needs no tzdata in the image)
FRESH_HOURS = 48
UA = {"User-Agent": "Mozilla/5.0 (compatible; StorylineBot/1.0)"}

# (group, label, yahoo symbol, unit)  — unit: "$" price, "%" yield, "" plain level
QUOTES = [
    ("Energy", "WTI crude", "CL=F", "$"), ("Energy", "Brent crude", "BZ=F", "$"), ("Energy", "Natural gas", "NG=F", "$"),
    ("Rates", "US 10-year yield", "^TNX", "%"), ("Rates", "US 5-year yield", "^FVX", "%"), ("Rates", "US 30-year yield", "^TYX", "%"),
    ("Currency", "US Dollar Index", "DX-Y.NYB", ""), ("Currency", "USD/INR", "USDINR=X", ""), ("Currency", "USD/CNY", "CNY=X", ""),
    ("Equities", "Nifty 50", "^NSEI", ""), ("Equities", "Sensex", "^BSESN", ""), ("Equities", "S&P 500", "^GSPC", ""),
    ("Equities", "Nasdaq", "^IXIC", ""), ("Equities", "Hang Seng", "^HSI", ""),
    ("Volatility", "India VIX", "^INDIAVIX", ""), ("Volatility", "VIX", "^VIX", ""),
    ("Metals", "Gold", "GC=F", "$"), ("Metals", "Copper", "HG=F", "$"),
    ("Themes", "Lithium ETF (LIT)", "LIT", "$"), ("Themes", "Defence ETF (ITA)", "ITA", "$"),
    ("Themes", "Semiconductor ETF (SOXX)", "SOXX", "$"), ("Themes", "Nvidia", "NVDA", "$"),
]

# Narrative sections, in the order they appear. `queries` feed Google News; `sections` merge into one heading.
THEMES = [
    ("Oil, gas & crude", ["crude oil price OPEC", "natural gas prices LNG"]),
    ("US politics & policy", ["White House Congress economy policy"]),
    ("Bond yields & the Fed", ["Federal Reserve rate Treasury yields"]),
    ("Tariffs & trade", ["tariffs trade deal"]),
    ("Dollar & rupee", ["dollar index rupee"]),
    ("Geopolitics, wars & shipping routes", ["Strait of Hormuz Red Sea shipping", "war ceasefire geopolitics markets"]),
    ("China's economy", ["China economy PBOC exports"]),
    ("Inflation", ["inflation CPI RBI Fed"]),
    ("Monsoon & agriculture", ["monsoon IMD rainfall kharif"]),
    ("AI, data centres, semiconductors, defence & lithium", ["AI data centre capex", "semiconductor chips Nvidia TSMC", "defence orders stocks", "lithium price battery"]),
]


# ------------------------------------------------------------------ data gathering

def fetch_quotes() -> list[dict]:
    """Latest level plus 1-day and 5-day change for every QUOTES entry (missing ones are skipped, never faked)."""
    import yfinance as yf

    symbols = [q[2] for q in QUOTES]
    df = yf.download(symbols, period="1mo", interval="1d", progress=False, group_by="ticker", auto_adjust=False, threads=True)
    out = []
    for group, label, sym, unit in QUOTES:
        try:
            s = df[sym]["Close"].dropna()
            if len(s) < 6:
                continue
            last, d1, d5 = float(s.iloc[-1]), float(s.iloc[-2]), float(s.iloc[-6])
            if unit == "%":  # yields move in basis points, not percent
                chg1, chg5, kind = (last - d1) * 100, (last - d5) * 100, "bp"
            else:
                chg1, chg5, kind = (last / d1 - 1) * 100, (last / d5 - 1) * 100, "pct"
            out.append({"group": group, "label": label, "symbol": sym, "unit": unit, "last": last,
                        "chg1": chg1, "chg5": chg5, "kind": kind, "asof": s.index[-1].date().isoformat()})
        except Exception:  # noqa: BLE001 — one missing ticker must not sink the briefing
            continue
    return out


def _google_news(query: str, limit: int = 6) -> list[dict]:
    from app.ai.news import google_news

    return google_news(query, days=2, limit=limit)


def fetch_headlines() -> dict[str, list[dict]]:
    """{theme title: [headline, ...]} — only items from the last FRESH_HOURS, newest first, de-duplicated."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=FRESH_HOURS)
    seen: set[str] = set()
    result: dict[str, list[dict]] = {}
    for title, queries in THEMES:
        bucket = []
        for q in queries:
            try:
                items = _google_news(q)
            except Exception as e:  # noqa: BLE001
                logger.warning("briefing: news query %r failed: %s", q, type(e).__name__)
                continue
            for it in items:
                key = re.sub(r"\W+", " ", it["title"].lower())[:80]
                if it["when"] >= cutoff and key not in seen and it["title"]:
                    seen.add(key)
                    bucket.append(it)
            time.sleep(0.4)
        bucket.sort(key=lambda x: x["when"], reverse=True)
        result[title] = bucket[:5]
    # Finnhub's general market news as one extra bucket (no extra key needed — the app already has one)
    key = os.environ.get("FINNHUB_API_KEY")
    if key:
        try:
            r = requests.get("https://finnhub.io/api/v1/news", params={"category": "general", "token": key}, timeout=20)
            r.raise_for_status()
            extra = []
            for n in r.json()[:40]:
                when = datetime.fromtimestamp(n["datetime"], tz=timezone.utc)
                k = re.sub(r"\W+", " ", n["headline"].lower())[:80]
                if when >= cutoff and k not in seen:
                    seen.add(k)
                    extra.append({"title": n["headline"], "source": n.get("source", ""), "url": n.get("url"), "when": when})
            result["Other top market headlines"] = extra[:5]
        except Exception as e:  # noqa: BLE001
            logger.warning("briefing: finnhub news failed: %s", type(e).__name__)
    return result


# ------------------------------------------------------------------ formatting (all numbers come from here)

def _fmt_level(q: dict) -> str:
    v = q["last"]
    if q["unit"] == "%":
        return f"{v:.2f}%"
    s = f"{v:,.2f}"
    return f"${s}" if q["unit"] == "$" else s


def _fmt_chg(v: float, kind: str) -> str:
    return f"{v:+.0f} bp" if kind == "bp" else f"{v:+.2f}%"


def snapshot_table(quotes: list[dict]) -> str:
    rows = ["| Market | Level | 1D | 5D |", "|---|---:|---:|---:|"]
    for q in quotes:
        rows.append(f"| {q['label']} | {_fmt_level(q)} | {_fmt_chg(q['chg1'], q['kind'])} | {_fmt_chg(q['chg5'], q['kind'])} |")
    return "\n".join(rows)


def _age(when: datetime) -> str:
    h = (datetime.now(timezone.utc) - when).total_seconds() / 3600
    return f"{int(h)}h ago" if h < 48 else f"{int(h // 24)}d ago"


def _number_headlines(headlines: dict[str, list[dict]]) -> tuple[str, dict[int, dict]]:
    """The numbered headline block shown to the model, plus {n: headline} for resolving citations later."""
    lines, index, n = [], {}, 0
    for title, items in headlines.items():
        lines.append(f"## {title}")
        if not items:
            lines.append("(no fresh headlines in the last 48h)")
        for it in items:
            n += 1
            index[n] = it
            lines.append(f"[{n}] {it['title']} — {it['source']} ({_age(it['when'])})")
        lines.append("")
    return "\n".join(lines), index


SYSTEM_PROMPT = """You write the daily market briefing for Storyline, a research app used mostly by Indian retail investors.
You are given (1) exact market quotes and (2) numbered headlines from the last 48 hours. Write ONLY from those.

Output Markdown, no preamble, no title, no closing remarks. Use exactly these sections, in this order, each as a `### ` heading:
{sections}
### Why it matters for Indian investors
### What to watch today

Rules:
- Each theme section: 1 to 3 short, factual sentences. Quote numbers only if they appear in the quotes or a headline. Cite headlines ONLY as numbers like [3] or [3, 4] right after the claim. Figures taken from the MARKET QUOTES need no citation — never write [Quote] or any other bracketed word.
- If a theme has no fresh headline and no relevant quote, write exactly: No major update in the last 48 hours.
- Never invent facts, figures, dates, quotes or sources. Never predict prices or give buy/sell/hold advice or price targets.
- "Why it matters" = 3 bullets linking the day's developments to Indian markets (INR, crude import bill, FII flows, sectors) — only where the data supports it.
- "What to watch today" = 3 bullets of concrete things (data releases, events, levels already in the data).
- Plain, calm tone. About 350-450 words in total."""


def _compose_prompt(date_label: str, quotes: list[dict], block: str) -> tuple[str, str]:
    sections = "\n".join(f"### {t}" for t, _ in THEMES)
    quote_lines = "\n".join(
        f"- {q['label']}: {_fmt_level(q)} (1D {_fmt_chg(q['chg1'], q['kind'])}, 5D {_fmt_chg(q['chg5'], q['kind'])}, as of {q['asof']})" for q in quotes
    )
    user = f"DATE (IST): {date_label}\n\nMARKET QUOTES (exact):\n{quote_lines}\n\nHEADLINES (numbered):\n{block}"
    return SYSTEM_PROMPT.format(sections=sections), user


def _tidy(text: str) -> str:
    """Drop stray non-numeric bracket tags the model sometimes adds (e.g. "[Quote]") — only [n] citations are real."""
    return re.sub(r"\s*\[(?![\d,\s]+\])[A-Za-z][^\]\n]{0,20}\](?!\()", "", text)


def _valid_narrative(text: str) -> bool:
    return text.count("### ") >= 8 and 600 <= len(text) <= 9000


def _generate_narrative(system: str, user: str) -> tuple[str | None, str | None]:
    """Try each platform model in turn. Returns (markdown, model label) or (None, None)."""
    from pydantic_ai import Agent

    for link in platform_chain():
        try:
            agent = Agent(model=link["model"], system_prompt=system)
            text = str(agent.run_sync(user).output).strip()
            text = _tidy(re.sub(r"^```(?:markdown)?\s*|\s*```$", "", text).strip())
            if _valid_narrative(text):
                return text, link["label"]
            logger.warning("briefing: %s returned an unusable narrative (%d chars)", link["label"], len(text))
        except Exception as e:  # noqa: BLE001 — quota/outage on one model → try the next
            logger.warning("briefing: %s failed: %s: %s", link["label"], type(e).__name__, str(e)[:200])
    return None, None


def build_briefing(now_ist: datetime | None = None) -> tuple[str, dict]:
    """(markdown body, meta). Pure assembly — does not touch the database."""
    now_ist = now_ist or datetime.now(IST)
    date_label = now_ist.strftime("%A %-d %B %Y")
    quotes = fetch_quotes()
    headlines = fetch_headlines()
    block, index = _number_headlines(headlines)

    narrative, model_label = None, None
    if quotes or any(headlines.values()):
        system, user = _compose_prompt(date_label, quotes, block)
        narrative, model_label = _generate_narrative(system, user)

    header = [f"**Daily market briefing — {date_label}**", "",
              "_Compiled automatically from live market quotes and the last 48 hours of headlines. A summary, not investment advice._", ""]
    snapshot = []
    if quotes:
        snapshot = ["### Market snapshot", snapshot_table(quotes),
                    "_US markets show the previous session's close; India and Asia are the latest quote._", ""]

    cited: list[int] = []
    body_parts: list[str] = []
    if narrative:
        # story first, the numbers table after it (the table is long and would otherwise bury the narrative)
        body_parts += [narrative, ""] + snapshot
        # models cite singly [3] or grouped [3, 4, 5] — accept both
        nums = {int(x) for grp in re.findall(r"\[([\d,\s]{1,40})\]", narrative) for x in re.split(r"[,\s]+", grp) if x.isdigit()}
        cited = sorted(n for n in nums if n in index)
    else:
        body_parts += snapshot
        body_parts += ["_The AI summary wasn't available today (model quota or outage), so here are the raw headlines instead._", ""]
        for title, items in headlines.items():
            if items:
                body_parts.append(f"### {title}")
                for it in items:
                    body_parts.append(f"- [{it['title']}]({it['url']}) — {it['source']}")

    if cited:
        body_parts += ["", "### Sources"]
        body_parts += [f"[{n}] [{index[n]['title']}]({index[n]['url']}) — {index[n]['source']}" for n in cited]
    parts = header + body_parts

    meta = {"date": now_ist.date().isoformat(), "model": model_label, "degraded": narrative is None,
            "headlines": sum(len(v) for v in headlines.values()), "quotes": len(quotes)}
    return "\n".join(parts), meta


# ------------------------------------------------------------------ the job

def run_daily_briefing(force: bool = False) -> int:
    """Post (or, with force, regenerate) today's briefing. Returns 1 if a post was written, 0 if one already existed."""
    today = datetime.now(IST).date().isoformat()
    with get_users_conn() as u:
        existing = u.execute("SELECT id FROM community_posts WHERE briefing_date = ?", (today,)).fetchone()
    if existing and not force:
        return 0

    body, meta = build_briefing()
    with get_users_conn() as u:
        if existing:
            u.execute("UPDATE community_posts SET body = ?, meta_json = ?, deleted = 0 WHERE id = ?", (body, json.dumps(meta), existing["id"]))
        else:
            u.execute(
                "INSERT INTO community_posts (user_id, kind, body, briefing_date, meta_json) VALUES (NULL, 'ai_daily', ?, ?, ?)",
                (body, today, json.dumps(meta)),
            )
        u.commit()
    logger.info("community briefing posted for %s (model=%s degraded=%s)", today, meta["model"], meta["degraded"])
    return 1
