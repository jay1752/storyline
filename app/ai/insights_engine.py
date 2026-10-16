"""The per-stock analysis that tracks each tracked stock's story over time, using the user's
stock-investment-analysis framework (Anil Sanadhya hard filters, Core/Steady/Emerging/Reserve/Trim-Exit buckets,
the three-speed exit check, multi-framework triangulation, evidence-based sizing).

Division of labour (the same rule as the rest of the app): CODE computes every number — the hard-filter
table, growth acceleration, OPM compression, promoter trend, valuation, portfolio weight — straight from the stored
fundamentals (app/ai/checklist.py). The MODEL reads those numbers plus the last few days of headlines and the prior
entries of this stock's story, and writes the judgement: verdict, bucket, exit-speed status, triangulation, what
changed. It is told never to invent a figure; anything we don't hold (interest coverage, current ratio, CFO/PAT) is
reported UNKNOWN.

Each run appends one row to position_snapshots (never overwrites), so the table IS the running story. Every attempt —
including failures — is recorded in insight_runs so the UI can say what happened."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.ai import errors
from app import framework as framework_rules
from app.ai.checklist import evaluate_framework
from app.ai.news import company_news
from app.ai.providers import build_chain
from app.journal import entries as journal_entries, rule_flags
from app.auth.middleware import current_user_id
from app.db import get_market_conn, get_users_conn

logger = logging.getLogger(__name__)

DISCLAIMER = ("Not SEBI-registered advice. This is your own framework applied by an AI to public data — it can be wrong. "
              "Verify the numbers before acting.")
VERDICT_TO_SIGNAL = {"BUY": "buy_consider", "HOLD": "hold", "TRIM": "trim_consider", "EXIT": "exit_consider", "WATCH": "watch"}

SYSTEM_PROMPT = """You are the analyst inside Tenetfolio, applying the user's personal stock-investment-analysis framework
(Anil Sanadhya's approach: Indian equities, business-driven, buy great companies, build slowly, hold long) to ONE stock per
run, and continuing that stock's running story from the prior entries you are shown.

NON-NEGOTIABLE HONESTY RULES
- Every number comes from the data block. Never invent, estimate or "recall" a figure. If a value is missing, say UNKNOWN.
- If a rule shows UNKNOWN in the data block (the figure is not in our data for this company), report it as unknown, never guess.
- The thresholds and position sizes below are THIS USER'S OWN settings (their Investment rules), not fixed doctrine: apply exactly these numbers.
<<off_rules>>
- You are not a SEBI-registered adviser; this is the user's framework applied to data.
- Lynch category, Mayer probability, Mukherjea fit and Dorsey moat are qualitative judgements: base them on the data given
  plus widely known facts about the business; if you are not sure, answer UNKNOWN rather than guess.

FRAMEWORK
Buckets (exactly one): Core (5+ yrs, all filters passing 3+ yrs; about <<size_target>>% cost, up to <<size_max>>%) · Steady (4+ consecutive quarters of
accelerating fundamentals; up to <<size_steady>>%) · Emerging (first crossed the filters in the last 2 quarters; <<size_start>>% starter, <<size_confirmed>>% after 2Q
confirmation) · Reserve (passes filters, watchlist, 0%) · Trim-Exit (1 quarter of breaking a filter; confirm at quarter 2 -> exit).
There is no trading bucket: once bought, hold until the BUSINESS deteriorates.
Hard filters: ROCE > <<roce>>% (ROE > <<roe>>% for banks/financials), D/E < <<de>> (n/a for banks), interest coverage > <<interest_cover>>,
promoter pledge < <<pledge>>% (above = avoid), current ratio > <<current_ratio>>, quick ratio > <<quick_ratio>>, sales growth > <<sales_growth>>% over the recent 4 quarters
(accelerating preferred), operating margin > <<opm>>% for high-margin businesses, promoter holding high and not falling by more than <<promoter>> pp.
Three-speed exit check:
- Speed 1 (immediate): auditor change/restatement, pledge suddenly > <<pledge>>%, SEBI/RBI ban or major enforcement, confirmed fraud/governance
  scandal. Only mark "triggered" if a supplied headline or data point actually shows it; otherwise "clear" (or "monitor" if a credible
  headline hints at it).
- Speed 2 (two consecutive quarters): ROCE < <<roce>>% (ROE < <<roe>>% banks), sales growth actually negative, OPM compression > 500 bps from
  peak, CFO/PAT < <<cfo_pat>>, working capital stretching > 30 days, D/E > <<de>>. One bad quarter = noise (status "one_quarter_warning");
  two = trend ("two_quarter_confirmed" -> exit within <<exit_days>> days).
- Speed 3 (12-18 months): promoter holding declining, moat erosion, sector regulatory change, capital-allocation drift.
NOT exit triggers: price falling alone, market/sector correction, ONE bad quarter, an expensive P/E (that means TRIM, not exit),
news headlines/political noise, analyst downgrades, stop-losses (never used).
Yellow flags (TRIM, not exit): position > <<size_watch>>% of portfolio (trim toward <<size_target>>%; above <<size_max>>% is an alert), valuation far ahead of growth, a sector > <<sector_max>>% of the book.
Sizing: never scale up without <<evidence_quarters>>-quarter evidence; never delay exit past 2-quarter confirmation; build over 1-2 years; never buy a
second lot in the same quarter as the first.
Realistic expectation: 14-17% INR CAGR over 5-10 years.

THE USER'S OWN JOURNAL (optional)
The user may log what THEY did with this stock: added more, trimmed, exited, or a note. When entries are shown they are ground
truth about the user's actions (not advice). Fold them into the story: refer to what they did and when ("you added 50 shares on
2 Sep"), judge whether each action fits the framework given the data, and check any thesis in their notes against the numbers.
Rule checks computed from the journal and position (same-quarter lots, averaging down, evidence before adding, position size,
late exit) are given to you — do NOT restate them, including position size. story_flags are ONLY for NEW observations about the
user's own actions or stated thesis measured against the data (e.g. "your note says margins would hold — they fell 700 bps",
"trimming while every filter passes cuts a winner"); general fundamentals belong in the other sections, not here. Add at most 3
(level ok/watch/warning/alert). Respect their actions in the verdict: if they added recently the <<evidence_quarters>>-quarter-evidence rule blocks
another BUY (say HOLD); if they trimmed, comment on whether the remaining size still fits the bucket. With no journal, set
user_story to an empty string and story_flags to an empty list — never invent user actions.

EVIDENCE, FRAMING AND STYLE
- State every figure with its period and unit on first use (Rs 1,813 Cr for FY2026; 20.7% in Q1 FY27). Say which price date a valuation statement rests on.
- Keep fact and inference apart: a view of yours reads "this suggests". Never present a paraphrase as a quote.
- The verdict is this framework's read of the user's OWN rules applied to the data. It is not an instruction to buy, sell or trim. Write "the framework reads
  this as HOLD because ..." rather than "you should ...". Analyst targets are other people's opinions: attribute them, never adopt them.
- If the market is not IN (a foreign-listed stock) this Indian-equity framework does not apply: give a descriptive read only, set verdict WATCH and bucket
  Reserve, and say in what_changed that it is descriptive.
- Never use em dashes or en dashes. Do not use: it's important to note, in today's, deep dive, delve, landscape, robust, compelling, navigate, poised to,
  well-positioned, exciting, multifaceted, tapestry, in conclusion, overall, it remains to be seen, game-changer, unlock value.

VALUE DRIVERS, CASH AND PEERS
- Name the 3 to 4 value drivers that move THIS business's earnings (pick the row of the table below that fits its business model), follow them through what_changed and
  metrics_to_watch, and put the ones we hold no figure for into data_gaps. Do not copy generic ratios.
- Profit versus cash: the data block carries CASH VS PROFIT. When operating cash flow stays under <<cfo_pat>> times profit, or profit grows while cash does not, say so
  plainly in what_changed or verdict_reasoning.
- Peers: the data block carries dated peer multiples. In verdict_reasoning give ONE sentence on where this stock sits against them and why a premium or discount might
  be justified (cash quality, scale, growth). Peer lists can include poor matches: ignore an obviously unrelated peer and say so. Method by business model: price to book
  for banks and lenders, earnings multiple for asset-light businesses, operating-profit multiple for capital-heavy ones.
- DATA NOTES carries things found by code (missing quarters, other income share of profit). Never read a missing quarter as a result.
<<driver_table>>

TRIGGERS
- upgrade_if and downgrade_if: BOTH lists must be filled, 2 to 4 measurable conditions each, tied to figures in the data block (for example "operating cash flow above
  profit for two more years"). upgrade_if is what would improve THIS framework read, downgrade_if what would make it worse (even when the read is already weak: say what
  would worsen it further). They are not price targets.
- The data block lists the triggers from the PREVIOUS entry. For each one give a trigger_check item: status fired, not_fired or unclear, with a one-line note from the
  data. With no previous triggers, leave trigger_check empty.
- data_gaps: the specific things we could not establish for this company, not generic caveats.

HOW TO WRITE THE ENTRY
- what_changed: 2-4 sentences, a CONTINUATION of the prior entries (what is new since the last check, or "no material change").
  Only when ENTRY NUMBER is 1 say it is the first entry and summarise the starting picture; otherwise never call it the first.
- verdict: BUY (scale up / initiate — only with the evidence the framework requires), HOLD, TRIM, EXIT or WATCH (not yet actionable).
  A watchlist stock (not owned) can be BUY / WATCH; an owned position HOLD / TRIM / EXIT / BUY(add).
- Cite news as [n] using only the numbered headlines supplied. Do not cite what you were not given.
- Keep every text field concise and specific; no filler, no hype, no price targets."""


def build_system_prompt(th: dict, sz: dict) -> str:
    """The analyst prompt with THIS USER's numbers filled in: their Investment rules (thresholds) and sizing settings."""
    from app import framework as fr
    vals = {**{k: v["value"] for k, v in th.items()}, **{**fr.sizing_defaults(), **(sz or {})}}
    off = [v["label"] for v in th.values() if not v.get("enabled", True)]
    from app.ai.textcheck import DRIVER_TABLE

    text = SYSTEM_PROMPT.replace("<<driver_table>>", DRIVER_TABLE).replace("<<off_rules>>", ("- Rules the user has switched OFF are not applied and must not be reported as failing: " + ", ".join(off) + ".") if off else "")
    for k, v in vals.items():
        text = text.replace(f"<<{k}>>", f"{v:g}" if isinstance(v, (int, float)) else str(v))
    return text


# ---------------------------------------------------------------------------- output schema

class SpeedOne(BaseModel):
    status: Literal["clear", "monitor", "triggered"]
    note: str


class SpeedTwo(BaseModel):
    status: Literal["clear", "monitor", "one_quarter_warning", "two_quarter_confirmed"]
    note: str


class SpeedThree(BaseModel):
    status: Literal["clear", "monitor", "confirming"]
    note: str


class Triangulation(BaseModel):
    lynch_category: Literal["Slow Grower", "Stalwart", "Fast Grower", "Cyclical", "Turnaround", "Asset Play", "Unknown"]
    mayer_probability: Literal["HIGH", "MEDIUM", "LOW", "UNKNOWN"]
    mayer_reason: str
    coffee_can: Literal["PASS", "FAIL", "UNKNOWN"]
    coffee_can_note: str
    moat_type: str
    moat_durability: Literal["Wide", "Narrow", "None", "Unknown"]


class TriggerCheck(BaseModel):
    trigger: str
    status: Literal["fired", "not_fired", "unclear"]
    note: str = ""


class StoryFlag(BaseModel):
    level: Literal["ok", "watch", "warning", "alert"]
    text: str


_SPEED_NOTE_ALIASES = ("details", "detail", "reason", "summary", "explanation", "comment", "text")


def _coerce_analysis(data):
    """Weaker / free models follow the JSON schema loosely: `details` instead of `note`, a sentence where an object belongs,
    too many list items. Normalise those here (the real fields and meanings are unchanged) rather than failing the whole run."""
    if not isinstance(data, dict):
        return data
    d = dict(data)
    for k in ("speed1", "speed2", "speed3"):
        v = d.get(k)
        if isinstance(v, str):
            d[k] = {"status": "monitor", "note": v}
        elif isinstance(v, dict):
            v = dict(v)
            if not v.get("note"):
                v["note"] = next((v.pop(a) for a in _SPEED_NOTE_ALIASES if v.get(a)), "")
            if isinstance(v.get("status"), str):
                v["status"] = v["status"].strip().lower().replace(" ", "_").replace("-", "_")
            d[k] = v
    t = d.get("triangulation")
    if isinstance(t, str):
        d["triangulation"] = {"lynch_category": "Unknown", "mayer_probability": "UNKNOWN", "mayer_reason": t, "coffee_can": "UNKNOWN",
                              "coffee_can_note": "", "moat_type": "Unknown", "moat_durability": "Unknown"}
    for k, n in (("metrics_to_watch", 4), ("news_takeaways", 4), ("data_gaps", 6), ("story_flags", 3), ("upgrade_if", 4), ("downgrade_if", 4), ("trigger_check", 8)):
        if isinstance(d.get(k), list):
            d[k] = d[k][:n]
    if isinstance(d.get("verdict"), str):
        d["verdict"] = d["verdict"].strip().upper()
    return d


class Analysis(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _tolerant(cls, data):
        return _coerce_analysis(data)

    verdict: Literal["BUY", "HOLD", "TRIM", "EXIT", "WATCH"]
    bucket: Literal["Core", "Steady", "Emerging", "Reserve", "Trim-Exit"]
    target_weight_pct: float | None = Field(None, description="Target portfolio weight at cost, percent; null if not applicable")
    what_changed: str
    growth_hook: str
    speed1: SpeedOne
    speed2: SpeedTwo
    speed3: SpeedThree
    triangulation: Triangulation
    sizing_note: str
    next_check: str
    metrics_to_watch: list[str] = Field(max_length=4)
    rerate_trigger: str
    verdict_reasoning: str
    news_takeaways: list[str] = Field(max_length=4)
    confidence: Literal["high", "medium", "low"]
    data_gaps: list[str] = Field(max_length=6)
    user_story: str = Field("", description="2-4 sentences on the user's own journal actions vs the framework; empty when there is no journal")
    story_flags: list[StoryFlag] = Field(default_factory=list, max_length=3)
    upgrade_if: list[str] = Field(default_factory=list, max_length=4, description="2-4 measurable conditions that would improve this framework read")
    downgrade_if: list[str] = Field(default_factory=list, max_length=4, description="2-4 measurable conditions that would weaken it")
    trigger_check: list[TriggerCheck] = Field(default_factory=list, max_length=8, description="status of each trigger from the previous entry")


class AnalysisError(Exception):
    """A run that could not produce an analysis. `quota` = every model failed on quota/credits (stop the batch)."""

    def __init__(self, message: str, quota: bool = False):
        super().__init__(message)
        self.quota = quota


# ---------------------------------------------------------------------------- gathering

def _r(v, n=1):
    return None if v is None else round(float(v), n)


def _quarter_table(rows: list[dict]) -> list[dict]:
    return [{
        "period": r["period_end_date"], "revenue": _r(r["revenue_cr"]), "yoy_growth_pct": _r(r["revenue_growth_pct"]),
        "opm_pct": _r(r["opm_pct"]), "net_profit": _r(r["net_profit_cr"]), "eps": _r(r["eps_adjusted"] if r["eps_adjusted"] is not None else r["eps"], 2),
        "roce_pct": _r(r["roce_pct"]), "roe_pct": _r(r["roe_pct"]), "debt_equity": _r(r["debt_equity"], 2),
        "promoter_pct": _r(r["promoter_pct"]), "pledge": r["promoter_pledge"], "fii_pct": _r(r["fii_pct"]),
    } for r in rows]


def position_weight(u, pos, price) -> float | None:
    """This position's share of the user's book (%), at latest prices; None for a watchlist stock or when quantity/price is unknown."""
    qty, sym = pos["quantity"], pos["symbol"]
    if pos["anchor_type"] != "position" or not qty or not price:
        return None
    from app import fx
    FX_INR_PER_USD = fx.rate()  # live USD->INR (hourly job), the same rate the Portfolio page uses
    total = 0.0
    with get_market_conn() as m:
        for p in u.execute("SELECT symbol, quantity FROM tracked_positions WHERE user_id = ? AND anchor_type = 'position' AND status = 'active' AND quantity IS NOT NULL", (pos["user_id"],)):
            row = m.execute("SELECT close FROM price_history WHERE symbol = ? ORDER BY date DESC LIMIT 1", (p["symbol"],)).fetchone()
            if row:
                total += p["quantity"] * row["close"] * (FX_INR_PER_USD if not p["symbol"].startswith("NSE:") else 1.0)
    mine = qty * price * (FX_INR_PER_USD if not sym.startswith("NSE:") else 1.0)
    return round(mine / total * 100, 1) if total else None


def _gather(u, pos) -> dict:
    """Everything the analysis needs about one tracked stock, computed in code. `u` is a users-DB connection."""
    from app.routes.analytics import _compute_pe, _yoy  # reuse the exact valuation / growth logic the Analytics page shows

    sym = pos["symbol"]
    stock = u.execute("SELECT name, sector, market FROM market.stocks WHERE symbol = ?", (sym,)).fetchone()
    if not stock:
        raise AnalysisError(f"{sym} is no longer in the stock universe.")
    allq = u.execute("SELECT * FROM market.quarterly_data WHERE symbol = ? ORDER BY period_end_date", (sym,)).fetchall()
    quarterly = [r for r in allq if "annual" not in (r["quarter_label"] or "").lower()]
    annual = [r for r in allq if "annual" in (r["quarter_label"] or "").lower()]
    if not quarterly and not annual:
        raise AnalysisError(f"There are no reported fundamentals for {sym} yet, so there is nothing to analyse.")

    # Fill a missing YoY growth from our own rows exactly like the Analytics page does, so filters and page agree.
    by_end = {r["period_end_date"]: r for r in quarterly}
    q_dicts = []
    for r in quarterly[-8:]:
        d = dict(r)
        if d.get("revenue_growth_pct") is None:
            d["revenue_growth_pct"] = _yoy(r, by_end, stored=False)
        q_dicts.append(d)

    from app.annual import annual_rows
    with get_market_conn() as _am:
        annual = annual_rows(_am, sym)
    th = framework_rules.resolve(u, pos["user_id"], sym)
    sz = framework_rules.sizing_plan(u, pos["user_id"])
    framework = evaluate_framework(q_dicts, stock["sector"], th, annual=annual)

    with get_market_conn() as m:
        closes = m.execute("SELECT date, close FROM price_history WHERE symbol = ? ORDER BY date DESC LIMIT 400", (sym,)).fetchall()
        pe_cur, pe_lo, pe_hi = _compute_pe(m, sym, quarterly[-8:]) if quarterly else (None, None, None)
    price = closes[0]["close"] if closes else None
    year = [c["close"] for c in closes[:260]]
    ago = next((c["close"] for c in closes if c["date"] <= (date.fromisoformat(closes[0]["date"]) - timedelta(days=365)).isoformat()), None) if closes else None
    valuation = {
        "price": _r(price, 2), "price_date": closes[0]["date"] if closes else None,
        "high_52w": _r(max(year), 2) if year else None, "low_52w": _r(min(year), 2) if year else None,
        "return_1y_pct": _r((price / ago - 1) * 100) if price and ago else None,
        "pe_current": pe_cur, "pe_3y_low": pe_lo, "pe_3y_high": pe_hi,
    }

    # the user's own position in this stock, and how big it is in their book
    qty, buy = pos["quantity"], pos["buy_price"]
    weight = position_weight(u, pos, price)
    position = {
        "type": pos["anchor_type"], "since": pos["anchor_date"], "quantity": qty, "avg_buy_price": buy,
        "return_since_buy_pct": _r((price / buy - 1) * 100) if price and buy else None, "weight_of_book_pct": weight,
    }

    prior = []
    for s in u.execute("SELECT id, checked_at, signal, valuation_label, reasoning_json, analysis_json FROM position_snapshots "
                       "WHERE position_id = ? ORDER BY id DESC LIMIT 5", (pos["id"],)).fetchall():
        a = json.loads(s["analysis_json"]) if s["analysis_json"] else None
        narrative = (a or {}).get("what_changed") or (json.loads(s["reasoning_json"] or "[]") or [""])[0]
        if not narrative or narrative.startswith("Just added"):
            continue
        story = re.sub(r"^\s*This is the first (entry|check|analysis)[^.]*\.\s*", "", narrative, flags=re.I) or narrative  # don't let "first entry" echo forward
        narrative = story
        prior.append({"date": s["checked_at"][:16] + " UTC", "verdict": (a or {}).get("verdict") or s["signal"], "bucket": (a or {}).get("bucket") or s["valuation_label"], "story": narrative})

    # the user's optional journal, and the framework rules their own actions can break (computed in code)
    events = journal_entries(u, pos["id"])
    prior_analyses = []
    for sn in u.execute("SELECT analysis_json FROM position_snapshots WHERE position_id = ? AND analysis_json IS NOT NULL ORDER BY id DESC LIMIT 40", (pos["id"],)):
        prior_analyses.append(json.loads(sn["analysis_json"]))
    q_all = []
    for r in quarterly:  # YoY growth filled from our own rows exactly as above, so the evidence check sees the same numbers
        d = dict(r)
        if d.get("revenue_growth_pct") is None:
            d["revenue_growth_pct"] = _yoy(r, by_end, stored=False)
        q_all.append(d)
    flags = rule_flags(events, pos, q_all, price, weight, prior_analyses, sz=sz, growth_bar=th["sales_growth"]["value"]) if (events or weight) else []

    entry_no = 1 + u.execute("SELECT COUNT(*) FROM position_snapshots WHERE position_id = ? AND analysis_json IS NOT NULL", (pos["id"],)).fetchone()[0]
    news = company_news(sym, stock["name"])
    extras = _extras(sym, stock["market"], quarterly, annual, prior_analyses)
    return {
        **extras,
        "entry_no": entry_no,
        "journal": events, "rule_flags": flags, "system": build_system_prompt(th, sz),
        "symbol": sym, "name": stock["name"], "sector": stock["sector"], "market": stock["market"],
        "position": position, "valuation": valuation, "framework": framework,
        "quarters": _quarter_table(q_dicts[-6:]),
        "annual": [{"fy": r["period_end_date"][:4], "revenue": _r(r["revenue_cr"]), "net_profit": _r(r["net_profit_cr"]), "opm_pct": _r(r["opm_pct"]),
                    "roce_pct": _r(r["roce_pct"]), "roe_pct": _r(r["roe_pct"]), "operating_cash_flow": _r(r.get("cfo_cr")), "free_cash_flow": _r(r.get("fcf_cr")),
                    "borrowings": _r(r.get("borrowings_cr")), "reserves": _r(r.get("reserves_cr")), "debtor_days": _r(r.get("debtor_days"), 0),
                    "inventory_days": _r(r.get("inventory_days"), 0), "payable_days": _r(r.get("days_payable"), 0)} for r in annual[-10:]],
        "news": news, "prior": list(reversed(prior)),
    }


def _extras(sym: str, market: str, quarterly: list, annual: list, prior_analyses: list) -> dict:
    """What the writer needs beyond the filters, all computed here: profit against cash, dated peers, notes found by code, and the triggers
    from the previous entry (so this entry can say which fired)."""
    from datetime import date as _date

    cash = []
    try:
        if market == "IN":
            for r in annual[-4:]:
                if r["cfo_cr"] is not None and r["net_profit_cr"]:
                    cash.append(f"FY{r['period_end_date'][:4]}: operating cash flow Rs {r['cfo_cr']:,.0f} Cr against net profit Rs {r['net_profit_cr']:,.0f} Cr = {r['cfo_cr'] / r['net_profit_cr']:.2f} times")
        else:
            with get_market_conn() as m:
                for r in m.execute("SELECT period_end_date, cfo_m, net_profit_m FROM us_annual WHERE symbol = ? ORDER BY period_end_date DESC LIMIT 4", (sym,)).fetchall()[::-1]:
                    if r["cfo_m"] is not None and r["net_profit_m"]:
                        cash.append(f"{r['period_end_date']}: operating cash flow USD {r['cfo_m']:,.0f} M against net profit USD {r['net_profit_m']:,.0f} M = {r['cfo_m'] / r['net_profit_m']:.2f} times")
    except Exception:  # noqa: BLE001 - optional context must never stop a run
        cash = []

    notes = []
    try:
        shown = quarterly[-8:]
        for a_, b_ in zip(shown, shown[1:]):
            gap = (_date.fromisoformat(b_["period_end_date"]) - _date.fromisoformat(a_["period_end_date"])).days
            if gap > 110:
                notes.append(f"{round(gap / 91) - 1} quarter(s) missing between {a_['period_end_date']} and {b_['period_end_date']}: growth for those periods may be absent, do not read the gap as a result")
        last4 = quarterly[-4:]
        if len(last4) == 4 and all(r["other_income_cr"] is not None and r["pbt_cr"] for r in last4):
            oi, pbt = sum(r["other_income_cr"] for r in last4), sum(r["pbt_cr"] for r in last4)
            if pbt > 0:
                notes.append(f"Other income over the last four quarters is {oi / pbt * 100:.0f}% of profit before tax ({oi:,.0f} of {pbt:,.0f})")
    except Exception:  # noqa: BLE001
        pass

    peers, peers_as_of = [], None
    try:
        from app.indianapi_db import INDIANAPI_DB, get_indianapi_conn
        ia = get_indianapi_conn()
        peers = [f"{p['peer_name']}: P/E {p['pe']:.1f}, P/B {p['pb']:.1f}, market cap Rs {p['market_cap']:,.0f} Cr" for p in ia.execute(
            "SELECT peer_name, pe, pb, market_cap FROM ia_peer WHERE symbol = ? AND pe > 0 AND pb > 0 ORDER BY market_cap DESC LIMIT 6", (sym,)).fetchall()]
        ia.close()
        peers_as_of = datetime.fromtimestamp(INDIANAPI_DB.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        peers = []

    prior_triggers = None
    for a in prior_analyses:   # newest first
        if a.get("upgrade_if") or a.get("downgrade_if"):
            prior_triggers = {"upgrade_if": a.get("upgrade_if") or [], "downgrade_if": a.get("downgrade_if") or []}
            break
    return {"cash_check": cash, "data_notes": notes, "peers": peers, "peers_as_of": peers_as_of, "prior_triggers": prior_triggers}


def _prompt(ctx: dict, today: str) -> str:
    fw = ctx["framework"]
    news_lines = "\n".join(
        f"[{i}] {n['title']} — {n['source']}" + (f" ({n['when'].date().isoformat()})" if n.get("when") else "")
        for i, n in enumerate(ctx["news"], 1)
    ) or "(no headlines found for the last 7 days)"
    prior = "\n".join(f"- {p['date']}: {p['verdict']} / {p['bucket']} — {p['story']}" for p in ctx["prior"]) or "none — this is the first entry for this stock"
    journal = "\n".join(
        f"- {e['event_date']} {e['event_type'].upper()}"
        + (f" {e['quantity']:g} shares" if e.get("quantity") else "") + (f" @ {e['price']:g}" if e.get("price") else "")
        + (f' — "{e["note"]}"' if e.get("note") else "")
        for e in ctx["journal"]
    ) or "none — the user has not logged any adds, trims, exits or notes for this stock"
    rules = "\n".join(f"- [{f['level']}] {f['text']}" for f in ctx["rule_flags"]) or "none triggered"
    cash = "\n".join(f"- {x}" for x in ctx.get("cash_check") or []) or "not available"
    peers = "\n".join(f"- {x}" for x in ctx.get("peers") or []) or "no peer data"
    notes = "\n".join(f"- {x}" for x in ctx.get("data_notes") or []) or "none"
    pt = ctx.get("prior_triggers")
    triggers = ("upgrade_if: " + "; ".join(pt["upgrade_if"]) + "\ndowngrade_if: " + "; ".join(pt["downgrade_if"])) if pt else "none (this is the first entry with triggers)"
    filters = "\n".join(
        f"- {f['label']}: {f['value'] if f['value'] is not None else 'UNKNOWN'} (needs {f['threshold']}) -> {f['status'].upper()}" + (f" [{f['note']}]" if f["note"] else "")
        for f in fw["filters"]
    )
    return f"""TODAY: {today}
ENTRY NUMBER: {ctx['entry_no']} for this stock ({'the first — say so' if ctx['entry_no'] == 1 else 'NOT the first: continue the story from the prior entries below and never call this the first entry'})
STOCK: {ctx['symbol']} — {ctx['name']} · sector: {ctx['sector']} · market: {ctx['market']}
USER'S POSITION: {json.dumps(ctx['position'])}

HARD-FILTER CHECK (computed by code from reported data):
{filters}
Mechanical bucket starting point: {fw['bucket_guess']}

EXIT-SPEED FACTS (computed): {json.dumps(fw['facts'])}

LAST QUARTERS (oldest first; revenue and profit in {'Rs crore' if ctx['market'] == 'IN' else 'USD million'}):
{json.dumps(ctx['quarters'])}

ANNUAL HISTORY (FY, oldest first, Rs crore; the 10-year ROCE consistency check, cash flow, borrowings and working-capital days; may be empty):
{json.dumps(ctx['annual'])}

VALUATION / PRICE: {json.dumps(ctx['valuation'])}

NEWS — last 7 days (numbered; cite only these):
{news_lines}

PRIOR ENTRIES OF THIS STOCK'S STORY (oldest first):
{prior}

USER'S OWN JOURNAL (optional; what the user says they did):
{journal}
RULE CHECKS ALREADY COMPUTED FROM THE JOURNAL AND POSITION (do not repeat):
{rules}

CASH VS PROFIT (computed by code):
{cash}

PEERS (IndianAPI peer list, as of {ctx.get('peers_as_of') or 'unknown date'}; may include poor matches):
{peers}
This stock's own P/E: {ctx['valuation'].get('pe_current')}

DATA NOTES (found by code):
{notes}

TRIGGERS FROM THE PREVIOUS ENTRY (give a trigger_check item for each):
{triggers}

Write today's entry."""


# ---------------------------------------------------------------------------- model call

_KIND_WORDS = {
    "size": ("portfolio", "book", "weight", "concentrat", "% of"),
    "same_quarter": ("same quarter", "second lot", "one lot", "2nd lot"),
    "averaging": ("averag",),
    "evidence": ("2-quarter", "two-quarter", "two quarters", "2 quarters", "evidence"),
    "late_exit": ("exit within", "30 days", "two-quarter deterioration", "confirmed deterioration"),
}


def _new_ai_flags(ai_flags, rule_flags: list[dict]) -> list[dict]:
    """The model's extra observations, minus any that restate a concept the code's rule flags already cover
    (the model is told not to repeat them, but this is the guarantee)."""
    covered = {r.get("kind") for r in rule_flags}
    out = []
    for f in ai_flags:
        t = f.text.lower()
        if any(k in covered and any(w in t for w in words) for k, words in _KIND_WORDS.items()):
            continue
        out.append({"level": f.level, "text": f.text, "source": "ai"})
    return out


def _structured_output_problem(e: Exception) -> bool:
    t = (type(e).__name__ + " " + str(e)).lower()
    return any(w in t for w in ("tool_use_failed", "tool call validation", "did not match schema", "unexpectedmodelbehavior",
                                "validation error", "does not support tool", "no endpoints found that support", "tool use"))


def _generate(chain: list[dict], prompt: str, system: str | None = None) -> tuple[Analysis, dict]:
    """Walk the user's model chain; transient errors get retries, anything else moves to the next model."""
    from pydantic_ai import Agent, PromptedOutput

    if not chain:
        raise AnalysisError("No usable AI model: add an API key in AI settings first.")
    last_exc, last_link, quota_only = None, None, True
    for link in chain:
        for attempt in range(3):
            try:
                try:
                    result = Agent(model=link["model"], system_prompt=system or SYSTEM_PROMPT, output_type=Analysis).run_sync(prompt)
                except Exception as first:  # noqa: BLE001
                    # Some models/providers (Groq's gpt-oss, assorted free OpenRouter models) reject or botch tool-call
                    # output ("tool call validation failed", wrong field names). Ask for plain JSON text instead and validate
                    # it ourselves, with the tolerant coercion above.
                    if not _structured_output_problem(first):
                        raise
                    logger.info("insights: %s can't do tool-call output (%s); retrying as prompted JSON", link["label"], type(first).__name__)
                    result = Agent(model=link["model"], system_prompt=system or SYSTEM_PROMPT, output_type=PromptedOutput(Analysis)).run_sync(prompt)
                return result.output, link
            except Exception as e:  # noqa: BLE001
                logger.warning("insights: %s failed (attempt %d): %s: %s", link["label"], attempt + 1, type(e).__name__, str(e)[:200])
                last_exc, last_link = e, link
                if attempt < 2 and errors.is_transient(e):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if not errors.is_quota(e):
                    quota_only = False
                break
    raise AnalysisError(errors.friendly_error(last_exc, last_link["label"]), quota=quota_only)


# ---------------------------------------------------------------------------- one stock

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def queue_run(user_id: int, position_id: int, trigger: str = "manual") -> tuple[int | None, bool]:
    """Create a queued run for one of the user's active positions. Returns (run_id, created): created is False when a run
    was already queued/running for that stock (its id is returned instead, so nothing is analysed twice). (None, False)
    if the position isn't theirs."""
    with get_users_conn() as u:
        if not u.execute("SELECT 1 FROM tracked_positions WHERE id = ? AND user_id = ? AND status = 'active'", (position_id, user_id)).fetchone():
            return None, False
        busy = u.execute("SELECT id FROM insight_runs WHERE position_id = ? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1", (position_id,)).fetchone()
        if busy:
            return busy["id"], False
        cur = u.execute("INSERT INTO insight_runs (position_id, user_id, trigger, status, started_at) VALUES (?,?,?, 'queued', ?)",
                        (position_id, user_id, trigger, _now()))
        u.commit()
        return cur.lastrowid, True


def execute_run(run_id: int) -> tuple[bool, bool]:
    """Do the work for a queued run (runs on a worker thread). Always leaves the run row in success/failed.
    Returns (succeeded, failed_on_quota)."""
    with get_users_conn() as u:
        run = u.execute("SELECT * FROM insight_runs WHERE id = ?", (run_id,)).fetchone()
        if not run:
            return False, False
        u.execute("UPDATE insight_runs SET status = 'running' WHERE id = ?", (run_id,))
        u.commit()
    current_user_id.set(run["user_id"])
    status, error = "success", None
    try:
        _analyse(run["user_id"], run["position_id"], run["trigger"])
    except AnalysisError as e:
        status, error = "failed", str(e)
        run_quota = e.quota
    except Exception as e:  # noqa: BLE001 — never leave a run stuck in 'running'
        logger.exception("insights run %s crashed", run_id)
        status, error, run_quota = "failed", "Unexpected error while analysing this stock. Try again.", False
    else:
        run_quota = False
    with get_users_conn() as u:
        u.execute("UPDATE insight_runs SET status = ?, error = ?, finished_at = ? WHERE id = ?", (status, error, _now(), run_id))
        u.commit()
    return status == "success", run_quota


def _analyse(user_id: int, position_id: int, trigger: str) -> None:
    chain = build_chain(user_id)
    with get_users_conn() as u:
        pos = u.execute("SELECT * FROM tracked_positions WHERE id = ? AND user_id = ? AND status = 'active'", (position_id, user_id)).fetchone()
        if not pos:
            raise AnalysisError("That tracked stock no longer exists.")
        ctx = _gather(u, pos)
        today = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%A %d %B %Y")
        prompt_text = _prompt(ctx, today)
        analysis, link = _generate(chain, prompt_text, ctx.get("system"))

        from app.ai import textcheck

        dashes = [0]
        a = textcheck.clean_strings(analysis.model_dump(), dashes)
        sources_text = prompt_text
        written = " ".join(textcheck.strings_of({k: a.get(k) for k in ("what_changed", "growth_hook", "verdict_reasoning", "sizing_note", "next_check", "rerate_trigger", "user_story",
                                                                         "metrics_to_watch", "news_takeaways", "data_gaps", "upgrade_if", "downgrade_if", "trigger_check", "story_flags")}))
        not_in, counted = textcheck.figures_not_in_sources(written, sources_text)
        a["checks"] = {"figures_counted": counted, "figures_not_in_sources": [round(x, 2) for x in dict.fromkeys(not_in)][:12], "banned_phrases": textcheck.banned_in(written), "dashes_removed": dashes[0]}
        a.update({
            "version": 2,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": link["label"],
            "filters": ctx["framework"]["filters"], "facts": ctx["framework"]["facts"], "bucket_guess": ctx["framework"]["bucket_guess"],
            "valuation": ctx["valuation"], "position": ctx["position"],
            "news": [{"n": i, "title": n["title"], "source": n["source"], "url": n["url"],
                      "when": n["when"].isoformat() if n.get("when") else None} for i, n in enumerate(ctx["news"], 1)],
            "disclaimer": DISCLAIMER,
            "journal": [{k: e[k] for k in ("event_date", "event_type", "quantity", "price", "note")} for e in ctx["journal"]],
        })
        # rule-based flags first (deterministic), then the model's additional observations
        a["story_flags"] = ctx["rule_flags"] + _new_ai_flags(analysis.story_flags, ctx["rule_flags"])
        price = ctx["valuation"]["price"]
        since_anchor = _r((price / pos["buy_price"] - 1) * 100, 2) if price and pos["buy_price"] else None
        reasoning = [analysis.what_changed]
        if analysis.speed1.status == "triggered":
            reasoning.append(f"Speed 1 exit trigger: {analysis.speed1.note}")
        if analysis.speed2.status in ("one_quarter_warning", "two_quarter_confirmed"):
            reasoning.append(f"Speed 2 ({analysis.speed2.status.replace('_', ' ')}): {analysis.speed2.note}")
        u.execute(
            """INSERT INTO position_snapshots
               (position_id, price_at_check, since_anchor_pct, valuation_label, signal, reasoning_json, trigger_type,
                evidence_json, news_summary, analysis_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (position_id, price, since_anchor, analysis.bucket, VERDICT_TO_SIGNAL[analysis.verdict], json.dumps(reasoning),
             "event" if trigger == "manual" else "interval", json.dumps(ctx["framework"]["filters"]),
             json.dumps([n["title"] for n in ctx["news"]]), json.dumps(a)),
        )
        u.commit()


# ---------------------------------------------------------------------------- batch (daily job / "analyse all")

def run_insights_for_user(user_id: int, progress=None, trigger: str = "daily", skip_fresh_hours: int = 18) -> int:
    """Analyse the active tracked stocks of one user, one after another: every stock for a manual run, the stocks the user's Investment
    rules select (scope, per-stock switch, daily cap) for the daily run. Stocks analysed within `skip_fresh_hours`
    are skipped, so a re-run after a partial day only does what is left. Stops early (without error) when every model
    is out of quota — remaining stocks are picked up by the next run. Returns how many analyses were written."""
    current_user_id.set(user_id)
    with get_users_conn() as u:
        positions = u.execute("SELECT id, symbol, anchor_type FROM tracked_positions WHERE user_id = ? AND status = 'active' ORDER BY anchor_type, symbol", (user_id,)).fetchall()
        if trigger == "daily":  # the automatic run follows the user's Investment rules (platform default, own override, per-stock switch)
            plan = framework_rules.story_plan(u, user_id)
            positions = framework_rules.story_select(plan, positions)
            skip_fresh_hours = plan["fresh"]
    if trigger == "daily" and not positions:
        return 0
    if not build_chain(user_id):
        raise AnalysisError("No usable AI model: add an API key in AI settings first.")
    with get_users_conn() as u:
        fresh_cut = (datetime.now(timezone.utc) - timedelta(hours=skip_fresh_hours)).strftime("%Y-%m-%d %H:%M:%S")
        fresh = {r["position_id"] for r in u.execute(
            "SELECT DISTINCT position_id FROM position_snapshots WHERE analysis_json IS NOT NULL AND checked_at > ?", (fresh_cut,))}
    written = 0
    for p in positions:
        if p["id"] in fresh:
            continue
        run_id, created = queue_run(user_id, p["id"], trigger)
        if not run_id or not created:
            continue
        ok, quota = execute_run(run_id)
        written += 1 if ok else 0
        if progress:
            progress(written)
        if not ok and quota:
            logger.info("insights: models out of quota, stopping this user's batch after %d analyses", written)
            break
        time.sleep(1.0)
    return written


def live_scan(u, user_id: int, symbol: str) -> dict | None:
    """The Fundamentals scan recomputed right now from stored data and the user's CURRENT rules — no AI, no stored snapshot,
    so editing a threshold on the story page changes the table immediately (the AI's own read updates at its next run)."""
    from app.routes.analytics import _yoy

    stock = u.execute("SELECT sector FROM market.stocks WHERE symbol = ?", (symbol,)).fetchone()
    allq = u.execute("SELECT * FROM market.quarterly_data WHERE symbol = ? ORDER BY period_end_date", (symbol,)).fetchall()
    quarterly = [r for r in allq if "annual" not in (r["quarter_label"] or "").lower()]
    if not stock or not quarterly:
        return None
    by_end = {r["period_end_date"]: r for r in quarterly}
    rows = []
    for r in quarterly[-8:]:
        d = dict(r)
        if d.get("revenue_growth_pct") is None:
            d["revenue_growth_pct"] = _yoy(r, by_end, stored=False)
        rows.append(d)
    from app.annual import annual_rows
    with get_market_conn() as _am:
        annual = annual_rows(_am, symbol)
    return evaluate_framework(rows, stock["sector"], framework_rules.resolve(u, user_id, symbol), annual=annual)
