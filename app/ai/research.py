"""Deep research: one long, sourced report on a tracked stock, started by the user with a button (never automatic).

Same division of labour as the daily story (app/ai/insights_engine.py): CODE gathers and computes every number into a numbered dossier (S1, S2 ...),
the MODEL writes the reading and must cite the source ids. Differences from the story: it is a standalone document, not a running log; it
reads everything in the Company section too (profile, officers, corporate actions, filings feed, news, peers, analyst view, named holders); and
it ends in measurable upgrade/downgrade triggers and an explicit list of what we do not know, never in a price target or a buy/sell instruction.

Version 1 reads our own data, the filings LIST (titles, dates, categories) and headlines. It does not yet open the filing PDFs (annual report,
call transcript, investor deck); the report says so on its face. After the model answers, code checks the text: numbers that appear nowhere in the
dossier are listed, banned phrases and dashes are removed or flagged, missing sections and unknown source ids are caught.

    queue_research(user_id, position_id) -> (report_id, created)      execute_research(report_id)  (runs on the worker pool)
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

from typing import Literal

from pydantic import BaseModel, Field

from app.ai import errors, textcheck
from app.ai.textcheck import BANNED, DRIVER_TABLE, banned_in
from app import framework as framework_rules
from app.ai.insights_engine import AnalysisError, _gather
from app.ai.providers import build_chain
from app.auth.middleware import current_user_id
from app.db import get_market_conn, get_users_conn
from app.indianapi_db import get_indianapi_conn

logger = logging.getLogger(__name__)

DISCLAIMER = ("Independent research built by software from public filings, exchange data and headlines, for information only. It is not investment advice, "
              "not an offer or solicitation, and it can be wrong. Check the figures against the filings before you act.")
_ONE_AT_A_TIME = threading.Semaphore(1)   # the 1 GB server has room for one long prompt at a time; the rest wait as "queued"

# (key, title, what the section must answer). Sections whose evidence we do not hold must say so, not fill the space.
SECTIONS = [
    ("business", "What the company does and how it gets paid", "The business in plain words, then the 4 to 6 numbers that actually move this company's earnings (pick from the value-driver table that matches its business model; list the ones we hold figures for)."),
    ("history", "How it got here", "Five-year picture from the annual figures and the company profile: what grew, what changed, and the cause. Use only what the sources show."),
    ("financials", "Financial history", "Sales, operating margin, profit and EPS over the years and the latest quarters, with units. Separate one-off effects (other income, exceptional items) if the sources show them."),
    ("cash", "Does reported profit turn into cash?", "Operating cash flow against profit for at least three years, free cash flow, and working-capital days. State plainly when profit grows but cash does not."),
    ("balance", "What could break the balance sheet?", "Borrowings against reserves, interest cover, current and quick ratio, and any pledge. If a lender's figures are missing, say they are missing."),
    ("people", "Who runs it and who owns it", "Named officers, promoter holding and its trend, pledge, and the largest named holders with their change. Note what the data cannot show (pay, incentives)."),
    ("promises", "What management promised and what happened", "If call transcripts or presentations are in the dossier: write 90 to 200 words of narrative that walks through the material statements management made (what, when, by whom), what the figures and later filings since show, and where they fell short or beat. The guidance list is shown separately under this section, so do not just point to it: the narrative must stand on its own and quote at most one or two short passages. If no document text was read, say so and use only the filings list, corporate actions and headlines."),
    ("growth", "What drives growth over the next two to three years", "From the analyst estimates if present (label them as analysts' views, with the number of analysts) and the company's own growth record. No forecast of your own."),
    ("better", "What could go better than the base case", "Specific levers, each with the figure that would confirm it."),
    ("worse", "What could go worse than the base case", "A ranked, specific bear case, each with the figure that would confirm it."),
    ("valuation", "Valuation against its own history and its peers", "Where today's multiple sits in its own three-year range and against the dated peers. Use the method that fits the business model (book value for banks and lenders, earnings multiple for asset-light businesses, operating-profit multiple for capital-heavy ones). Analyst targets are other people's opinion, say so. Give no price target."),
    ("catalysts", "Dated catalysts", "Upcoming results, board meetings, AGM and ex-dividend dates from the sources, each with its date."),
    ("rules", "How it measures against your rules", "Each of the user's own rules: value, threshold, pass or fail. Then one sentence on what that mix means."),
]

SYSTEM_PROMPT = """You are the research writer inside Tenetfolio, an app used mostly by Indian retail investors. You write one decision-grade research
report on ONE listed company, for a user who tracks it and asked for the long version. A numbered dossier (S1, S2 ...) holds everything you may use.

HONESTY RULES (non-negotiable)
- Every number comes from the dossier. Never invent, estimate or recall a figure. If it is not there, it is UNKNOWN: write that, and list it under unknowns.
- After a claim that rests on a source, cite it as [S3] or [S2, S7]. Cite only ids that exist in the dossier. A fact with no source id is a sign it should not be there.
- Label a view of yours as inference ("this suggests") and keep it apart from facts. A quote is verbatim with its source; otherwise it is a paraphrase and must read as one.
- Analyst targets, ratings and estimates are other people's opinions: say whose, and how many analysts.
- You do not give a price target, a buy, sell or hold instruction, or a personal recommendation. You give a valuation-versus-price read and measurable triggers.
- Not every section can be filled from this dossier. Where evidence is missing, say what is missing in one sentence and move on. An honest gap is better than a guess.

<<evidence>>

HOW TO WRITE
- Plain English, long direct sentences, one causal thread from the first section to the last. Stop when the point is made.
- Exact figures with period and unit on first use in each section (Rs 1,813 Cr for FY2026, 20.7% in Q1 FY27). Indian companies in Rs crore, US companies in USD million.
- Never use em dashes or en dashes. Use periods, commas or colons. No hype, no filler.
- Never use these phrases: it's important to note, in today's, in the current environment, deep dive, delve, landscape, robust, compelling, navigate, poised to,
  well-positioned, exciting, remarkable journey, multifaceted, tapestry, in conclusion, overall, it remains to be seen, game-changer, unlock value, easy to miss,
  the real story, you are betting, here's the catch, let that sink in.
- Do not write paragraphs. Each section has: lead (ONE sentence, at most 35 words, that answers the section's question), points (3 to 5 short points, each one fact or one inference,
  at most 32 words, with figures, units, periods and [S#] cites) and figures (2 to 4 key numbers for a side panel, each a label and a value copied exactly from the dossier, for example
  label "Operating cash flow, FY2026" and value "-Rs 4,142 Cr"). Leave body empty. Write the sections in the order and with the keys you are given.
- The report may be saved as a PDF and shared. Outside the "rules" section never mention the user's own rules, their position, their journal, their buy price or what they hold. Write
  neutrally instead ("the quick ratio is 0.2x", not "fails your filter")
- Open with one dek sentence (the business, the picture, what would change it) and exactly 3 takeaways, at least one saying what would change the view. The dek, takeaways and valuation_read
  must never mention the user's rules, thresholds or position: they are the shareable front page.
- upgrade_if and downgrade_if: each item is a measurable condition tied to a figure in the dossier ("operating cash flow above profit for two more years"), 2 to 4 items each.
- unknowns: the specific things we could not establish (not generic caveats).
- If the security is foreign-listed, write an explainer only: business, financials, risks, valuation context, and leave upgrade_if and downgrade_if empty.

""" + DRIVER_TABLE


class Fig(BaseModel):
    label: str
    value: str


class Sec(BaseModel):
    key: str
    title: str = ""
    lead: str = ""
    points: list[str] = Field(default_factory=list)
    figures: list[Fig] = Field(default_factory=list)
    body: str = ""            # older reports and models that ignore the structure: plain paragraphs
    cites: list[str] = Field(default_factory=list)


class Guidance(BaseModel):
    said_on: str = Field(description="date of the call or document, YYYY-MM-DD")
    speaker: str = Field(description="name and role as printed in the transcript, or 'company presentation'")
    quote: str = Field(description="word for word from the document, at most 40 words")
    topic: str
    outcome: str = Field(description="what the figures and later filings in the dossier show since, with source ids; or 'not yet known'")
    status: Literal["pending", "met", "beat", "missed", "partial", "unclear"]
    source: str = Field(description="source id of the document, for example S14")


class Report(BaseModel):
    dek: str
    takeaways: list[str]
    sections: list[Sec]
    valuation_read: str
    upgrade_if: list[str] = Field(default_factory=list)
    downgrade_if: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    guidance: list[Guidance] = Field(default_factory=list, description="4 to 8 material management statements from the documents with what happened since; empty if no document text was read")


def system_prompt(meta: dict) -> str:
    docs = meta.get("documents") or []
    if docs:
        listing = "; ".join(f"{d['source_id']} {d['label']}" for d in docs)
        evidence = (f"DOCUMENTS READ\nThe dossier includes the extracted text of: {listing}. You may quote them. A quote is word for word and SHORT: at most 25 words (anything longer is cut by the system), with the speaker (call transcripts name "
                    "their speakers) and the source id, for example: Ajoy Chawla said \"...\" [S15]. If you are not certain of the exact words, paraphrase and say \"in paraphrase\". Never attribute a "
                    "statement to a person unless the transcript text shows it. Extraction can drop a table or a chart: do not infer a figure that is not in the text.\n"
                    "The annual report was NOT read. The filings list gives titles, dates and categories for everything else.\n"
                    "Fill the guidance list with 4 to 8 of the most material statements management made (targets, plans, expectations), each with its date, speaker, verbatim quote, "
                    "what the figures and later filings in the dossier show since (cite ids), and a status: pending if the period is not complete, met, beat, missed or partial against what was said, "
                    "unclear if the dossier cannot tell.")
    else:
        evidence = ("EVIDENCE LIMITS OF THIS RUN\nNo filing documents (annual report, call transcripts, presentations) were read. The filings feed gives titles, dates and categories only. "
                    "Never describe what a call or a filing \"said\". Leave the guidance list empty and say in the promises section that no documents were read.")
    return SYSTEM_PROMPT.replace("<<evidence>>", evidence)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _f(v, places=1, suffix=""):
    if v is None:
        return "n/a"
    if isinstance(v, (int, float)):
        return f"{v:,.{places}f}{suffix}"
    return str(v)


# ------------------------------------------------------------------------------------------------------------------------------ the dossier
class Dossier:
    """Numbered sources plus their text. add() returns the id so the block can be written with it."""

    def __init__(self):
        self.sources: list[dict] = []
        self.blocks: list[str] = []

    def add(self, title: str, publisher: str, as_of: str | None, lines: list[str], note: str = "") -> str | None:
        lines = [x for x in lines if x]
        if not lines:
            return None
        sid = f"S{len(self.sources) + 1}"
        self.sources.append({"id": sid, "title": title, "publisher": publisher, "as_of": as_of, "note": note})
        self.blocks.append(f"[{sid}] {title} (publisher: {publisher}" + (f"; as of {as_of}" if as_of else "") + ")\n" + "\n".join(lines))
        return sid

    @property
    def text(self) -> str:
        return "\n\n".join(self.blocks)


def _file_date(path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d")
    except OSError:
        return None


def build_dossier(user_id: int, pos, set_id: int | None = None) -> tuple[Dossier, dict]:
    """Gather everything we hold about the stock into numbered sources. Returns (dossier, meta) where meta carries the reference price and flags."""
    from app.annual import annual_rows
    from app.indianapi_db import INDIANAPI_DB

    with get_users_conn() as u:
        rs = framework_rules.owned_set(u, user_id, set_id) if set_id else None   # a screener picked for this run (must be the user's own)
        if not rs and not set_id:
            a = framework_rules.assigned_set(u, user_id, pos["symbol"])          # else the one this stock follows, else their own rules
            rs = framework_rules.owned_set(u, user_id, a) if a else None
        ctx = _gather(u, pos, rs["id"] if rs else None)
    sym, market = ctx["symbol"], ctx["market"]
    cur = "Rs" if market == "IN" else "USD"
    unit = "Cr" if market == "IN" else "M"
    d = Dossier()
    meta = {"symbol": sym, "name": ctx["name"], "market": market, "sector": ctx["sector"], "gaps": [], "rules_name": rs["name"] if rs else None}

    with get_market_conn() as m:
        qrows = [r for r in m.execute("SELECT * FROM quarterly_data WHERE symbol = ? ORDER BY period_end_date", (sym,)).fetchall()
                 if "annual" not in (r["quarter_label"] or "").lower()]
        arows = annual_rows(m, sym) if market == "IN" else []
        us_ann = m.execute("SELECT * FROM us_annual WHERE symbol = ? ORDER BY period_end_date", (sym,)).fetchall() if market == "US" else []
        met = m.execute("SELECT * FROM stock_metrics WHERE symbol = ?", (sym,)).fetchone()

    # S1 quarterly results
    lines = [f"Basis: as stored for this company (consolidated where the source gave both). Amounts in {cur} {unit}. Columns: period | revenue | YoY % | operating margin % | net profit | EPS | finance cost"]
    by_end = {r["period_end_date"]: r for r in qrows}
    from app.routes.analytics import _yoy
    for r in qrows[-12:]:
        g = r["revenue_growth_pct"] if r["revenue_growth_pct"] is not None else _yoy(r, by_end, stored=False)
        lines.append(f"{r['period_end_date']} | {_f(r['revenue_cr'], 0)} | {_f(g)} | {_f(r['opm_pct'])} | {_f(r['net_profit_cr'], 0)} | {_f(r['eps'], 2)} | {_f(r['finance_cost_cr'], 0)}")
    # a quarter missing from the middle of the series would read as a collapse or a jump; say so in the dossier
    shown = qrows[-12:]
    missing_q = []
    for a_, b_ in zip(shown, shown[1:]):
        gap_days = (date.fromisoformat(b_["period_end_date"]) - date.fromisoformat(a_["period_end_date"])).days
        if gap_days > 110:
            n_missing = round(gap_days / 91) - 1
            missing_q.append(f"{n_missing} quarter(s) missing between {a_['period_end_date']} and {b_['period_end_date']}")
    if missing_q:
        lines.append("NOTE (found by code): " + "; ".join(missing_q) + ". Year-on-year growth for those periods may be absent. Do not read the gap as a result.")
        meta["gaps"].append("Quarterly series has missing quarters: " + "; ".join(missing_q))
    s_q = d.add("Quarterly results, last 12 quarters", "Exchange filings via Tenetfolio's database", qrows[-1]["period_end_date"] if qrows else None, lines if qrows else [])
    if not qrows:
        meta["gaps"].append("No quarterly results on file")

    # S2 annual results and cash flow (+ S13 computed checks below)
    ann_lines, conv_lines = [], []
    if arows:
        ann_lines.append(f"Amounts in Rs Cr. Columns: FY | revenue | operating margin % | net profit | EPS | operating cash flow | free cash flow | borrowings | reserves | ROCE % | ROE % | debtor days | inventory days | payable days")
        for r in arows[-10:]:
            ann_lines.append(f"FY{r['period_end_date'][:4]} | {_f(r['revenue_cr'], 0)} | {_f(r['opm_pct'])} | {_f(r['net_profit_cr'], 0)} | {_f(r['eps'], 2)} | {_f(r['cfo_cr'], 0)} | {_f(r['fcf_cr'], 0)} | "
                             f"{_f(r['borrowings_cr'], 0)} | {_f(r['reserves_cr'], 0)} | {_f(r['roce_pct'])} | {_f(r['roe_pct'])} | {_f(r['debtor_days'], 0)} | {_f(r['inventory_days'], 0)} | {_f(r['days_payable'], 0)}")
        for r in arows[-6:]:
            if r["cfo_cr"] is not None and r["net_profit_cr"]:
                conv_lines.append(f"FY{r['period_end_date'][:4]}: operating cash flow {_f(r['cfo_cr'], 0)} against net profit {_f(r['net_profit_cr'], 0)} = {r['cfo_cr'] / r['net_profit_cr']:.2f} times")
    elif us_ann:
        ann_lines.append("Amounts in USD million. Columns: FY end | revenue | operating profit | net profit | operating cash flow | total assets | equity | dividends paid")
        for r in us_ann[-10:]:
            ann_lines.append(f"{r['period_end_date']} | {_f(r['revenue_m'], 0)} | {_f(r['operating_profit_m'], 0)} | {_f(r['net_profit_m'], 0)} | {_f(r['cfo_m'], 0)} | {_f(r['assets_m'], 0)} | {_f(r['equity_m'], 0)} | {_f(r['dividends_paid_m'], 0)}")
        for r in us_ann[-6:]:
            if r["cfo_m"] is not None and r["net_profit_m"]:
                conv_lines.append(f"{r['period_end_date']}: operating cash flow {_f(r['cfo_m'], 0)} against net profit {_f(r['net_profit_m'], 0)} = {r['cfo_m'] / r['net_profit_m']:.2f} times")
    else:
        meta["gaps"].append("No annual figures on file")
    d.add("Annual results, cash flow and balance-sheet items", "Company filings via IndianAPI and SEC, in Tenetfolio's database" if market == "IN" else "SEC 10-K filings, in Tenetfolio's database",
          (arows[-1]["period_end_date"] if arows else (us_ann[-1]["period_end_date"] if us_ann else None)), ann_lines)

    # S3 price and valuation
    v = ctx["valuation"]
    pv = [f"Reference price {cur} {_f(v.get('price'), 2)} on {v.get('price_date')}. 52-week high {_f(v.get('high_52w'), 2)}, low {_f(v.get('low_52w'), 2)}. One-year return {_f(v.get('return_1y_pct'))}%."]
    if v.get("pe_current"):
        pv.append(f"P/E {_f(v.get('pe_current'))} against its own three-year range {_f(v.get('pe_3y_low'))} to {_f(v.get('pe_3y_high'))}.")
    if met:
        pv.append(f"Market cap {_f(met['mcap'], 0)} {'Rs Cr' if market == 'IN' else 'USD M'}; size tier {met['cap_tier'] or 'n/a'}.")
    d.add("Price and valuation", "Exchange prices via Tenetfolio's database", v.get("price_date"), pv)
    meta["ref_price"], meta["ref_date"], meta["currency"] = v.get("price"), v.get("price_date"), cur

    # S4 the user's own rules
    fl = [f"{f['label']}: {f['value'] if f['value'] is not None else 'UNKNOWN'} against {f['threshold']} -> {f['status'].upper()}" + (f" ({f['note']})" if f.get("note") else "") for f in ctx["framework"]["filters"]]
    d.add("The user's own investment rules, applied to the latest data", "Tenetfolio, using the user's thresholds" + (f" (screener: {rs['name']})" if rs else ""), meta["ref_date"], fl)

    # S5 company profile, S6 corporate actions (the Company section)
    company = None
    try:
        from app.ia_panels import company_panel
        company = company_panel(sym) if market == "IN" else None
    except Exception as e:  # noqa: BLE001
        logger.info("research: company panel skipped: %s", e)
    ia_date = _file_date(INDIANAPI_DB)
    if company:
        pl = []
        if company.get("description"):
            pl.append("Business description: " + re.sub(r"\s+", " ", company["description"])[:900])
        if company.get("industry"):
            pl.append(f"Industry: {company['industry']}")
        for o in company.get("officers") or []:
            pl.append(f"Officer: {o['name']}, {o['title']}, age {o.get('age') or 'n/a'}, since {o.get('since') or 'n/a'}")
        for metric, hz in (company.get("growth") or {}).items():
            if metric in ("Compounded Sales Growth", "Compounded Profit Growth", "Stock Price CAGR"):
                pl.append(f"{metric}: " + ", ".join(f"{k} {_f(x)}%" for k, x in hz.items() if x is not None))
        for g in (company.get("metrics") or [])[:8]:
            rows = "; ".join(f"{r['name']} {r['value']}" for r in g["rows"][:8])
            pl.append(f"Key metrics, {g['title']}: {rows}")
        d.add("Company profile, leadership and key metrics", "IndianAPI", ia_date, pl)
        acts = company.get("actions") or {}
        cl = [f"Board meeting {m['date']}: {m['text']}" for m in acts.get("meetings", [])]
        cl += [f"Dividend {x['date']}: {x['text']}" for x in acts.get("dividends", [])]
        cl += [f"{x['kind']} {x['date']}: {x['text']}" for x in acts.get("other", [])]
        d.add("Corporate actions", "IndianAPI (exchange corporate-action data)", ia_date, cl)
    elif market == "IN":
        meta["gaps"].append("No company profile or corporate actions on file")

    # S7 the filings we keep (NSE) and the text of the documents worth reading; BSE's 45-day feed is only the fallback
    meta["documents"], meta["doc_text"] = [], {}
    index_done = False
    if market == "IN":
        try:
            from app import filings as fil

            got = fil.refresh_key_documents(sym)
            rows = [r for r in fil.latest(sym, None, 60) if r["kind"] not in ("newspaper", "schedule", "audio")][:30]
            if rows:
                fl2 = [f"{r['filed_at'][:10]} | {r['kind']} | {(r['title'] or r['category'] or '')[:150]}" for r in rows]
                d.add("Exchange filings kept for this company, last 13 months (titles and kinds only; the documents marked as read below are included in full)", "NSE corporate announcements",
                      rows[0]["filed_at"][:10], fl2)
                index_done = True
            caps = {"transcript": 60000, "presentation": 30000, "results": 28000}
            labels = {"transcript": "Earnings call transcript", "presentation": "Investor presentation", "results": "Financial results filing (outcome of board meeting)"}
            for doc in got["read"]:
                txt = fil.text_of(doc["id"]) if doc["text_status"] == "ok" else None
                if not txt:
                    meta["gaps"].append(f"Could not read the {labels.get(doc['kind'], doc['kind'])} filed {doc['filed_at'][:10]} ({doc['text_status']})")
                    continue
                trimmed = txt[:caps.get(doc["kind"], 20000)]
                lab = f"{labels.get(doc['kind'], doc['kind'])}, filed {doc['filed_at'][:10]}"
                sid = d.add(f"{lab} (document text, extracted from the PDF" + (", first part only" if len(trimmed) < len(txt) else "") + ")", "The company, via NSE", doc["filed_at"][:10],
                            [trimmed], note=doc["url"])
                if sid:
                    meta["documents"].append({"source_id": sid, "kind": doc["kind"], "label": lab, "filed_at": doc["filed_at"][:10], "url": doc["url"], "chars": len(trimmed)})
                    meta["doc_text"][sid] = trimmed
            if got["sync"].get("error") and not rows:
                meta["gaps"].append("The exchange filings list could not be fetched")
        except Exception as e:  # noqa: BLE001
            logger.info("research: filings store skipped: %s", e)
    if not index_done:
        try:
            from app.announcements import get_announcements
            ann = get_announcements(sym, limit=25) if market == "IN" else {"items": []}
            fl2 = [f"{i['at'][:10]} | {i.get('category') or 'n/a'} | {i['title']}" + (f" | {i['summary']}" if i.get("summary") else "") for i in ann.get("items", [])[:25]]
            d.add("Exchange filings feed, last 45 days (titles and categories only; the documents themselves were not read)", "BSE announcements", (ann.get("as_of") or "")[:10] or None, fl2)
        except Exception as e:  # noqa: BLE001
            logger.info("research: filings feed skipped: %s", e)

    # S8 shareholding and named holders
    sl = []
    for r in qrows[-8:]:
        if r["promoter_pct"] is not None:
            sl.append(f"{r['period_end_date']}: promoters {_f(r['promoter_pct'], 2)}%, FII {_f(r['fii_pct'], 2)}%, DII {_f(r['dii_pct'], 2)}%, promoter pledge {_f(r['promoter_pledge_pct'] if r['promoter_pledge_pct'] is not None else r['promoter_pledge'])}")
    try:
        with get_market_conn() as m:
            per = m.execute("SELECT DISTINCT period_end FROM holders WHERE symbol = ? ORDER BY period_end DESC LIMIT 2", (sym,)).fetchall()
            if per and not m.execute("SELECT 1 FROM holders WHERE symbol=? AND period_end=? AND category='Needs check' LIMIT 1", (sym, per[0][0])).fetchone():
                cur_h = {r["holder_name"].casefold(): r for r in m.execute("SELECT holder_name, category, pct FROM holders WHERE symbol=? AND period_end=? AND category!='Promoter group'", (sym, per[0][0]))}
                prev_h = {r["holder_name"].casefold(): r["pct"] for r in m.execute("SELECT holder_name, pct FROM holders WHERE symbol=? AND period_end=?", (sym, per[1][0]))} if len(per) > 1 else {}
                prom = m.execute("SELECT COUNT(*), SUM(pct) FROM holders WHERE symbol=? AND period_end=? AND category='Promoter group'", (sym, per[0][0])).fetchone()
                if prom and prom[0]:
                    sl.append(f"Exchange filing for {per[0][0]}: promoter group of {prom[0]} holders holds {_f(prom[1], 2)}%")
                for k, r in sorted(cur_h.items(), key=lambda kv: -(kv[1]["pct"] or 0))[:8]:
                    ch = f", change {r['pct'] - prev_h[k]:+.2f} points against the previous filing" if k in prev_h else ""
                    sl.append(f"Named holder above 1% ({r['category']}): {r['holder_name'].title() if r['holder_name'].isupper() else r['holder_name']} {_f(r['pct'], 2)}%{ch}")
    except Exception as e:  # noqa: BLE001
        logger.info("research: holders skipped: %s", e)
    d.add("Shareholding and named holders", "Exchange shareholding filings (NSE) and IndianAPI", qrows[-1]["period_end_date"] if qrows else None, sl)

    # S9 peers, S10 analyst view
    try:
        ia = get_indianapi_conn()
        peers = ia.execute("SELECT peer_name, pe, pb, market_cap FROM ia_peer WHERE symbol=? ORDER BY market_cap DESC", (sym,)).fetchall()
        pl2 = [f"{p['peer_name']}: P/E {_f(p['pe'])}, P/B {_f(p['pb'], 2)}, market cap {_f(p['market_cap'], 0)} Rs Cr" for p in peers[:8]]
        if pl2 and met and met["pe"]:
            pl2.append(f"This company: P/E {_f(met['pe'])}")
        d.add("Listed peers (peer lists are IndianAPI's and can include poor matches; judge each)", "IndianAPI", ia_date, pl2)
        t = ia.execute("SELECT mean, high, low, n_estimates FROM ia_target WHERE symbol=?", (sym,)).fetchone()
        snaps = {r["age"]: r["mean"] for r in ia.execute("SELECT age, mean FROM ia_target_snapshot WHERE symbol=?", (sym,))}
        rt = ia.execute("SELECT rating_name, n_now, n_3m FROM ia_rating WHERE symbol=? AND rating_name!='Total' ORDER BY rating_value", (sym,)).fetchall()
        al = []
        if t and t["mean"]:
            al.append(f"Analysts' 12-month price target (their opinion): mean {_f(t['mean'], 0)}, high {_f(t['high'], 0)}, low {_f(t['low'], 0)}, from {t['n_estimates']} analysts"
                      + (f"; the mean was {_f(snaps['NinetyDaysAgo'], 0)} 90 days ago" if snaps.get("NinetyDaysAgo") else ""))
        if rt:
            al.append("Analyst ratings now against 3 months ago: " + ", ".join(f"{r['rating_name']} {r['n_now']} (was {r['n_3m']})" for r in rt))
        est = ia.execute("SELECT measure, fiscal_year, mean, high, low, n_estimates FROM ia_estimate WHERE symbol=? AND freq='Annual' AND kind='estimate' ORDER BY fiscal_year LIMIT 9", (sym,)).fetchall()
        names = {"EPS": "EPS", "SAL": "Sales", "NET": "Net profit"}
        for r in est:
            al.append(f"Analyst estimate, {names.get(r['measure'], r['measure'])} FY{r['fiscal_year']}: mean {_f(r['mean'], 2)}, range {_f(r['low'], 2)} to {_f(r['high'], 2)}, {r['n_estimates']} analysts")
        d.add("Analyst consensus (other people's opinions)", "IndianAPI", ia_date, al)
        ia.close()
    except Exception as e:  # noqa: BLE001
        logger.info("research: peers/analysts skipped: %s", e)

    # S11 news
    nl = [f"{n['when'].date().isoformat() if n.get('when') else 'n/a'} | {n['source']} | {n['title']}" for n in ctx["news"]]
    d.add("Headlines, last 7 days (headline text only, articles not read)", "News feeds", date.today().isoformat(), nl)

    # S12 the user's position and the app's own earlier story entries
    pos_lines = [f"Position: {json.dumps(ctx['position'])}"]
    for e in ctx["journal"]:
        pos_lines.append(f"Journal {e['event_date']} {e['event_type']}" + (f" {e['quantity']:g} shares" if e.get("quantity") else "") + (f" at {e['price']:g}" if e.get("price") else "") + (f": {e['note']}" if e.get("note") else ""))
    for p in ctx["prior"][-3:]:
        pos_lines.append(f"Earlier Tenetfolio story entry {p['date']}: {p['verdict']} / {p['bucket']}. {p['story'][:400]}")
    d.add("The user's position, journal and earlier story entries", "The user and Tenetfolio", None, pos_lines)

    # S13 computed checks
    cc = list(conv_lines)
    try:
        last4 = qrows[-4:]
        if len(last4) == 4 and all(r["other_income_cr"] is not None and r["pbt_cr"] for r in last4):
            oi, pbt = sum(r["other_income_cr"] for r in last4), sum(r["pbt_cr"] for r in last4)
            if pbt > 0:
                cc.append(f"Other income over the last four quarters is {oi / pbt * 100:.0f}% of profit before tax ({_f(oi, 0)} of {_f(pbt, 0)}), a hint of how much profit is not from operations")
    except Exception:  # noqa: BLE001
        pass
    d.add("Checks computed by code", "Tenetfolio", meta["ref_date"], cc)
    meta["glance"] = _glance(ctx, met, arows, qrows, us_ann, meta, v)
    meta["catalysts"] = _catalysts(company)
    return d, meta


def _money_big(x, market: str) -> str:
    if x is None:
        return "n/a"
    if market == "IN":
        return f"Rs {x / 1e5:.2f} lakh Cr" if x >= 1e5 else f"Rs {x:,.0f} Cr"
    return f"USD {x / 1e3:,.1f} B" if x >= 1e3 else f"USD {x:,.0f} M"


def _glance(ctx, met, arows, qrows, us_ann, meta, v) -> list[dict]:
    """The at-a-glance tiles, all computed here (never by the model): price, P/E in its own range, return on capital, margin, cash conversion, promoters, size and, for the screen only, the user's rules."""
    cur, market = meta["currency"], ctx["market"]
    bank = bool(met and met["bank"])
    t: list[dict] = []
    ret = v.get("return_1y_pct")
    if v.get("price"):
        t.append({"label": "Price", "value": f"{cur} {v['price']:,.2f}", "sub": (f"{ret:+.1f}% over 1 year, " if ret is not None else "") + f"as of {v.get('price_date')}",
                  "tone": "pos" if ret and ret > 0 else ("neg" if ret and ret < 0 else "")})
    pe, lo, hi = v.get("pe_current"), v.get("pe_3y_low"), v.get("pe_3y_high")
    if pe:
        sub = f"3-year range {lo:g} to {hi:g}" if lo and hi else ""
        if lo and hi and hi > lo:
            sub += f", {max(0, min(100, (pe - lo) / (hi - lo) * 100)):.0f}% of the way up"
        t.append({"label": "P/E", "value": f"{pe:g}x", "sub": sub, "tone": ""})
    if arows:
        r = arows[-1]
        key, lab = ("roe_pct", "ROE") if bank else ("roce_pct", "ROCE")
        if r[key] is not None:
            t.append({"label": lab, "value": f"{r[key]:.1f}%", "sub": f"FY{r['period_end_date'][:4]}", "tone": "pos" if r[key] >= 15 else ""})
    if qrows and qrows[-1]["opm_pct"] is not None:
        t.append({"label": "Operating margin", "value": f"{qrows[-1]['opm_pct']:.1f}%", "sub": f"quarter ended {qrows[-1]['period_end_date']}", "tone": ""})
    if arows and not bank:
        r = next((x for x in reversed(arows) if x["cfo_cr"] is not None and x["net_profit_cr"]), None)
        if r:
            ratio = r["cfo_cr"] / r["net_profit_cr"]
            t.append({"label": "Cash vs profit", "value": f"{ratio:.2f}x", "sub": f"operating cash flow over net profit, FY{r['period_end_date'][:4]}", "tone": "pos" if ratio >= 0.8 else "neg"})
    if qrows and qrows[-1]["promoter_pct"] is not None:
        t.append({"label": "Promoters", "value": f"{qrows[-1]['promoter_pct']:.1f}%", "sub": f"holding at {qrows[-1]['period_end_date']}", "tone": ""})
    if met and met["mcap"]:
        t.append({"label": "Market cap", "value": _money_big(met["mcap"], market), "sub": ((met["cap_tier"] or "").capitalize() + " cap") if met["cap_tier"] else "", "tone": ""})
    fl = [f for f in ctx["framework"]["filters"] if f["status"] in ("pass", "fail")]
    if fl:
        ok = sum(1 for f in fl if f["status"] == "pass")
        t.append({"label": "Your rules", "value": f"{ok} of {len(fl)} pass", "sub": f"against {meta['rules_name']}" if meta.get("rules_name") else "against your own thresholds", "tone": "pos" if ok == len(fl) else "", "personal": True})
    return t


def _catalysts(company) -> list[dict]:
    """Dated items from the corporate-action data: board meetings, dividends and their dates, other actions. Newest first."""
    out: list[dict] = []
    if not company:
        return out
    acts = company.get("actions") or {}
    for m in acts.get("meetings", []):
        if m.get("date"):
            out.append({"date": m["date"][:10], "text": (m.get("text") or "Board meeting")[:160], "kind": "Board meeting"})
    for x in acts.get("dividends", [])[:3]:
        if x.get("date"):
            out.append({"date": x["date"][:10], "text": (x.get("text") or "Dividend")[:160], "kind": "Dividend"})
    for x in acts.get("other", [])[:3]:
        if x.get("date"):
            out.append({"date": x["date"][:10], "text": f"{x.get('kind') or ''} {x.get('text') or ''}".strip()[:160], "kind": x.get("kind") or "Corporate action"})
    out.sort(key=lambda c: c["date"], reverse=True)
    return out[:8]


def visuals_for(meta: dict) -> dict:
    """The charts and data blocks for each section, built from our own tables at the moment the report is written, so the report is a fixed document."""
    from app import viz
    from app.routes.analytics import _lab

    v = viz.build(meta["symbol"], {"market": meta["market"], "sector": meta.get("sector")}, _lab, last_price=meta.get("ref_price"))
    out: dict[str, list] = {}

    def add(key, block):
        out.setdefault(key, []).append(block)

    if v.get("growth"):
        add("financials", {"type": "svg", "title": "Sales, growth and operating margin by quarter", "svg": v["growth"]["svg"], "read": v["growth"].get("read")})
    if v.get("cash"):
        add("cash", {"type": "svg", "title": "Net profit against operating cash flow, by year", "svg": v["cash"]["svg"], "read": v["cash"].get("read"), "legend": "cash"})
    if v.get("cycle"):
        add("cash", {"type": "svg", "title": "Days of stock, debtors and payables", "svg": v["cycle"]["svg"], "read": v["cycle"].get("read")})
    if v.get("capital"):
        add("balance", {"type": "svg", "title": "Return on capital employed, by year", "svg": v["capital"]["svg"], "read": v["capital"].get("read")})
        if v["capital"].get("debt"):
            add("balance", {"type": "debt", **v["capital"]["debt"]})
    own = v.get("own") or {}
    if own.get("area"):
        add("people", {"type": "svg", "title": "Shareholding by quarter", "svg": own["area"]["svg"], "read": own["area"].get("read")})
    if own.get("holders"):
        add("people", {"type": "holders", **own["holders"]})
    if v.get("rel"):
        add("valuation", {"type": "svg", "title": f"The stock against {v['rel'].get('bench')}, both set to 100", "svg": v["rel"]["svg"], "read": v["rel"].get("read")})
    val = v.get("value") or {}
    if val.get("pe"):
        add("valuation", {"type": "gauge", **val["pe"]})
    if val.get("analysts"):
        add("valuation", {"type": "analysts", **val["analysts"], "currency": meta.get("currency")})
    if v.get("peers"):
        add("valuation", {"type": "svg", "title": "Peers by P/E and P/B, bubble size is market cap", "svg": v["peers"]["svg"], "read": None})
    return out


# ------------------------------------------------------------------------------------------------------------------------------ prompt, model
def build_prompt(d: Dossier, meta: dict, today: str) -> str:
    guide = "\n".join(f"{i}. key \"{k}\", title \"{t}\": {g}" for i, (k, t, g) in enumerate(SECTIONS, 1))
    foreign = "FOREIGN-LISTED: write an explainer only; leave upgrade_if and downgrade_if empty.\n" if meta["market"] != "IN" else ""
    return f"""TODAY: {today}
COMPANY: {meta['symbol']} | {meta['name']} | sector {meta['sector']} | market {meta['market']}
REFERENCE PRICE: {meta['currency']} {meta.get('ref_price')} on {meta.get('ref_date')}. Every comparison to price is against this one anchor.
{foreign}KNOWN GAPS IN OUR DATA: {'; '.join(meta['gaps']) or 'none flagged by code'}

SOURCE DOSSIER (cite ids like [S2]; nothing outside it may be used as fact)

{d.text}

WRITE THE REPORT with these sections, in this order, using these keys:
{guide}

Then valuation_read (2 to 4 sentences: where the price sits against its own history and the peers, and what the price already seems to expect; no target), upgrade_if, downgrade_if and unknowns."""


def _generate(chain: list[dict], prompt: str, system: str) -> tuple[Report, dict]:
    from pydantic_ai import Agent, PromptedOutput

    if not chain:
        raise AnalysisError("No usable AI model: add an API key in AI settings first.")
    last_exc, last_link, quota_only = None, None, True
    for link in chain:
        for attempt in range(3):
            try:
                try:
                    result = Agent(model=link["model"], system_prompt=system, output_type=Report).run_sync(prompt)
                except Exception as first:  # noqa: BLE001
                    t = (type(first).__name__ + " " + str(first)).lower()
                    if not any(w in t for w in ("tool_use_failed", "tool call validation", "did not match schema", "unexpectedmodelbehavior", "validation error", "does not support tool", "tool use")):
                        raise
                    result = Agent(model=link["model"], system_prompt=system, output_type=PromptedOutput(Report)).run_sync(prompt)
                return result.output, link
            except Exception as e:  # noqa: BLE001
                logger.warning("research: %s failed (attempt %d): %s: %s", link["label"], attempt + 1, type(e).__name__, str(e)[:200])
                last_exc, last_link = e, link
                if attempt < 2 and errors.is_transient(e):
                    time.sleep(2 * (attempt + 1))
                    continue
                if not errors.is_quota(e):
                    quota_only = False
                break
    raise AnalysisError(errors.friendly_error(last_exc, last_link["label"]), quota=quota_only)


# ------------------------------------------------------------------------------------------------------------------------------ checks
def finalise(rep: Report, d: Dossier, meta: dict) -> dict:
    """Turn the model's answer into the stored report, with the automatic checks. Nothing here changes a figure."""
    ids = {s["id"] for s in d.sources}
    dashes = 0
    fixed = [0]

    def cl(x: str) -> str:
        nonlocal dashes
        y, n = textcheck.clean_dashes(x or "")
        y, k = textcheck.style_fix(y)
        dashes += n
        fixed[0] += k
        return y.strip()

    sections = []
    for sec in rep.sections:
        lead = cl(sec.lead)
        points = [cl(p) for p in sec.points if (p or "").strip()][:6]
        body = cl(sec.body)
        figs = [{"label": cl(f.label), "value": cl(f.value)} for f in sec.figures if (f.label or "").strip() and (f.value or "").strip()][:5]
        text = " ".join([lead, *points, body])
        if not body:
            body = " ".join([lead, *points]).strip()          # a plain-text form for the checks and for readers of old exports
        inline = re.findall(r"\[(S[\d, S]+)\]", text)
        cites = [c for c in dict.fromkeys(re.findall(r"S\d+", " ".join(sec.cites))) if c in ids] or [c for grp in inline for c in re.findall(r"S\d+", grp) if c in ids]
        cites = list(dict.fromkeys(cites + [c for grp in inline for c in re.findall(r"S\d+", grp) if c in ids]))
        sections.append({"key": sec.key, "title": cl(sec.title), "lead": lead, "points": points, "figures": figs, "body": body, "cites": cites})
    out = {"dek": cl(rep.dek), "takeaways": [cl(t) for t in rep.takeaways][:3], "sections": sections, "valuation_read": cl(rep.valuation_read),
           "upgrade_if": [cl(t) for t in rep.upgrade_if], "downgrade_if": [cl(t) for t in rep.downgrade_if], "unknowns": [cl(t) for t in rep.unknowns]}

    full = " ".join([out["dek"], *out["takeaways"], *(" ".join([s["lead"], *s["points"], s["body"], *(f["value"] for f in s["figures"])]) for s in sections), out["valuation_read"],
                     *out["upgrade_if"], *out["downgrade_if"]])
    unmatched, total = textcheck.figures_not_in_sources(full, d.text)
    total = total or 1
    banned = banned_in(full)
    expected = [k for k, _, _ in SECTIONS]
    missing = [k for k in expected if k not in {s["key"] for s in sections}]
    # quotes: every quoted passage (6+ words) and every guidance quote must appear word for word in a document we read
    def norm(t: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()

    doc_norm = norm(" ".join(meta.get("doc_text", {}).values()))

    def verified(q: str) -> bool:
        parts = [norm(x) for x in re.split(r"\.\.\.|\u2026|\[\.\.\.\]", q) if len(norm(x).split()) >= 4]
        return bool(parts) and all(x in doc_norm for x in parts) if doc_norm else False

    guidance = []
    for g in rep.guidance[:8]:
        q_text, cut = textcheck.first_words((g.quote or "").strip(), 40)
        gd = {"said_on": cl(g.said_on), "speaker": cl(g.speaker), "quote": q_text, "quote_cut": cut, "topic": cl(g.topic), "outcome": cl(g.outcome), "status": g.status,
              "source": g.source if g.source in ids else None}
        gd["quote_verified"] = verified(gd["quote"])
        guidance.append(gd)
    inline_quotes = [q for sec in sections for q in re.findall(r"[\"\u201c]([^\"\u201d]{30,400})[\"\u201d]", " ".join([sec["lead"], *sec["points"], sec["body"]])) if len(q.split()) >= 6]
    quotes_not_found = [q[:120] for q in inline_quotes if not verified(q)]
    out["guidance"] = guidance
    own = re.compile(r"\b(the user|your (own )?(rules?|thresholds?|position|filters?)|user'?s)\b", re.I)
    out["takeaway_personal"] = [bool(own.search(t)) for t in out["takeaways"]]
    out["glance"] = meta.get("glance") or []
    out["catalysts"] = meta.get("catalysts") or []
    out["sources"] = d.sources
    out["meta"] = {k: meta.get(k) for k in ("symbol", "name", "market", "sector", "ref_price", "ref_date", "currency", "gaps", "rules_name")}
    out["checks"] = {"numbers_total": total, "numbers_not_in_sources": [round(x, 2) for x in dict.fromkeys(unmatched)][:15], "numbers_not_in_sources_pct": round(100 * len(unmatched) / total),
                     "banned_phrases": banned, "dashes_removed": dashes, "phrases_replaced": fixed[0], "missing_sections": missing,
                     "documents_read": [{k: x[k] for k in ("source_id", "kind", "label", "filed_at", "url")} for x in meta.get("documents", [])],
                     "quotes_not_found": quotes_not_found, "guidance_quotes_unverified": sum(1 for g in guidance if not g["quote_verified"]),
                     "note": ("The annual report was not read. Documents read: " + "; ".join(x["label"] for x in meta.get("documents", [])) + ".") if meta.get("documents")
                             else "No filing documents were read for this report: it uses our data, the filings list and headlines."}
    out["disclaimer"] = DISCLAIMER
    return out


# ------------------------------------------------------------------------------------------------------------------------------ jobs
MANUAL_PER_DAY = 5


def queue_research(user_id: int, position_id: int, rule_set_id: int | None = None) -> tuple[int | None, bool]:
    with get_users_conn() as u:
        pos = u.execute("SELECT symbol FROM tracked_positions WHERE id = ? AND user_id = ? AND status = 'active'", (position_id, user_id)).fetchone()
        if not pos:
            return None, False
        busy = u.execute("SELECT id FROM research_reports WHERE position_id = ? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1", (position_id,)).fetchone()
        if busy:
            return busy["id"], False
        if rule_set_id and not framework_rules.owned_set(u, user_id, rule_set_id):
            rule_set_id = None
        cur = u.execute("INSERT INTO research_reports (user_id, position_id, symbol, status, created_at, rule_set_id) VALUES (?,?,?, 'queued', ?, ?)",
                        (user_id, position_id, pos["symbol"], _now(), rule_set_id))
        u.commit()
        return cur.lastrowid, True


def used_today(user_id: int) -> int:
    with get_users_conn() as u:
        return u.execute("SELECT COUNT(*) FROM research_reports WHERE user_id = ? AND created_at > datetime('now', '-1 day')", (user_id,)).fetchone()[0]


def execute_research(report_id: int) -> None:
    with get_users_conn() as u:
        row = u.execute("SELECT * FROM research_reports WHERE id = ?", (report_id,)).fetchone()
    if not row:
        return
    with _ONE_AT_A_TIME:
        with get_users_conn() as u:
            u.execute("UPDATE research_reports SET status = 'running' WHERE id = ?", (report_id,))
            u.commit()
        current_user_id.set(row["user_id"])
        status, error, model, payload = "success", None, None, None
        try:
            chain = build_chain(row["user_id"])
            with get_users_conn() as u:
                pos = u.execute("SELECT * FROM tracked_positions WHERE id = ? AND user_id = ?", (row["position_id"], row["user_id"])).fetchone()
            if not pos:
                raise AnalysisError("That tracked stock no longer exists.")
            d, meta = build_dossier(row["user_id"], pos, row["rule_set_id"])
            today = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%A %d %B %Y")
            rep, link = _generate(chain, build_prompt(d, meta, today), system_prompt(meta))
            model = link["label"]
            payload = finalise(rep, d, meta)
            try:
                payload["visuals"] = visuals_for(meta)
            except Exception:  # noqa: BLE001 - charts are a bonus; the written report stands without them
                logger.exception("research visuals skipped")
                payload["visuals"] = {}
            payload["generated_at"] = datetime.now(timezone.utc).isoformat()
            payload["model"] = model
        except AnalysisError as e:
            status, error = "failed", str(e)
        except Exception as e:  # noqa: BLE001 - never leave a report stuck in 'running'
            logger.exception("research report %s crashed", report_id)
            status, error = "failed", "Unexpected error while writing this report. Try again."
        with get_users_conn() as u:
            u.execute("UPDATE research_reports SET status = ?, error = ?, model = ?, report_json = ?, finished_at = ? WHERE id = ?",
                      (status, error, model, json.dumps(payload) if payload else None, _now(), report_id))
            u.commit()
