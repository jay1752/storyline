"""Exchange filings, kept and readable: the list per stock (NSE corporate announcements) and the text of the documents worth reading.

Why this exists: the old Recent-filings cache (app/announcements.py) holds the last 45 days and is overwritten on every refresh, so history never builds up
and nothing inside a filing was ever read. Here every filing seen is KEPT (table `filings`), classified (call transcript, investor presentation, results, press
release ...), and for the few that carry real evidence the PDF's text is extracted once and stored (`filing_text`). The deep-research report and the chat read
from this store.

NSE is the source because its announcements feed and its PDF archive both work from a plain server (BSE's API answers 403). Politeness: one request a second,
one shared session, PDFs capped by size, pages and time. Company filings are public documents; we keep the text for analysis and show only short quotes.

    sync(symbol, days=400)                  -> list the filings, keep them
    refresh_key_documents(symbol)           -> sync, then read the documents the report needs
    latest(symbol, kind, n)                 -> rows;  text_of(filing_id) -> str | None
"""
from __future__ import annotations

import io
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

import requests

from app.db import get_market_conn

logger = logging.getLogger(__name__)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
API = "https://www.nseindia.com/api/corporate-announcements"
MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_PAGES = {"transcript": 150, "results": 60, "presentation": 45, "press_release": 20, "board_outcome": 12, "annual_report": 0, "other": 30}
TIME_BUDGET_S = 75           # per document: a slow deck stops here with the pages read so far
_lock = threading.Lock()
_sess: dict = {"s": None, "at": 0.0, "last_call": 0.0}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _session() -> requests.Session:
    """One shared NSE session (cookies from the home page), re-created when it is old."""
    if _sess["s"] is None or time.time() - _sess["at"] > 900:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Referer": "https://www.nseindia.com/"})
        s.get("https://www.nseindia.com/", timeout=25)
        _sess.update(s=s, at=time.time())
    return _sess["s"]


def _gap() -> None:
    wait = 1.0 - (time.time() - _sess["last_call"])
    if wait > 0:
        time.sleep(wait)
    _sess["last_call"] = time.time()


def classify(desc: str, text: str, url: str) -> str:
    """What kind of document this filing is, from the category, the exchange's one-line text and the file name."""
    t = f"{desc} {text}".lower()
    f = (url or "").rsplit("/", 1)[-1].lower()
    if "transcript" in t or "transcript" in f:
        return "transcript"
    if "recording" in t or "audio" in f or "webcast" in t or "link to access" in t:
        return "audio"
    if "newspaper" in t or "newspaper" in f or "newpaper" in f:
        return "newspaper"                       # the results printed as an advertisement: a summary, not the filing
    if "investor presentation" in t or "presentation" in f or "earning presentation" in f:
        return "presentation"
    if ("annual report" in t or "annualreport" in f) and "letter" not in t and "intimation" not in f:
        return "annual_report"
    if "outcome of board meeting" in t:
        return "results" if "result" in t else "board_outcome"   # a results filing carries the tables and the auditor's report; a plain outcome may or may not be one
    if "press release" in t or "pressrelease" in f:
        return "press_release"
    if "schedule of" in t or ("intimation" in f and "meet" in f):
        return "schedule"
    return "other"


def sync(symbol: str, days: int = 400) -> dict:
    """Fetch the stock's announcements for the last `days` and keep every one. Returns {"seen", "new", "error"}. Never raises."""
    nse = symbol.split(":", 1)[-1]
    today = date.today()
    q = f"?index=equities&symbol={requests.utils.quote(nse)}&from_date={(today - timedelta(days=days)).strftime('%d-%m-%Y')}&to_date={today.strftime('%d-%m-%Y')}"
    with _lock:
        try:
            data = None
            for attempt in range(2):
                _gap()
                r = _session().get(API + q, timeout=40)
                if r.status_code == 200 and r.text[:1] in "[{":
                    data = r.json()
                    break
                _sess["s"] = None   # blocked or odd answer: start a fresh session and try once more
            if data is None:
                return {"seen": 0, "new": 0, "error": "NSE did not answer with a filings list"}
        except Exception as e:  # noqa: BLE001
            logger.info("filings sync %s failed: %s", symbol, e)
            return {"seen": 0, "new": 0, "error": f"{type(e).__name__}"}
    items = data if isinstance(data, list) else data.get("data", [])
    now, new = _now(), 0
    with get_market_conn() as m:
        for x in items:
            url = x.get("attchmntFile")
            if not url:
                continue
            fid = url.rsplit("/", 1)[-1]
            filed = (x.get("sort_date") or x.get("an_dt") or "")[:19]
            try:
                filed = datetime.strptime(x.get("an_dt", ""), "%d-%b-%Y %H:%M:%S").strftime("%Y-%m-%d %H:%M")
            except ValueError:
                filed = (x.get("sort_date") or "")[:16] or filed
            kind = classify(x.get("desc") or "", x.get("attchmntText") or "", url)
            cur = m.execute("INSERT OR IGNORE INTO filings (id, symbol, filed_at, category, title, url, kind, source, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,'nse',?,?)",
                            (fid, symbol, filed, x.get("desc"), (x.get("attchmntText") or "")[:400], url, kind, now, now))
            if cur.rowcount:
                new += 1
            else:
                m.execute("UPDATE filings SET last_seen = ?, kind = ? WHERE id = ?", (now, kind, fid))   # a better classification corrects old rows
        m.commit()
    return {"seen": len(items), "new": new, "error": None}


def _extract(content: bytes, kind: str) -> tuple[str, int]:
    """Text of the first pages of a PDF. pypdf, not pdfplumber: on a 14 MB, 70-page investor deck pdfplumber peaked at about 1 GB of memory (the app is capped at
    640 MB on the server) while pypdf needs about 90 MB and two seconds, and it keeps a transcript's speaker labels and line structure."""
    from pypdf import PdfReader

    cap = MAX_PAGES.get(kind, 30)
    deadline = time.time() + TIME_BUDGET_S
    parts = []
    rd = PdfReader(io.BytesIO(content))
    pages = len(rd.pages)
    for i in range(min(pages, cap)):
        if time.time() > deadline:
            logger.info("filing extraction stopped at page %d of %d (time budget)", i, pages)
            break
        try:
            parts.append(rd.pages[i].extract_text() or "")
        except Exception:  # noqa: BLE001 - one unreadable page must not lose the rest
            parts.append("")
    return "\n".join(parts), pages


def ensure_text(fid: str) -> str:
    """Make sure the filing's text is stored. Returns the final text_status."""
    with get_market_conn() as m:
        row = m.execute("SELECT * FROM filings WHERE id = ?", (fid,)).fetchone()
        if not row:
            return "failed"
        if row["text_status"] == "ok" and m.execute("SELECT 1 FROM filing_text WHERE id = ?", (fid,)).fetchone():
            return "ok"
    status, text, pages = "failed", "", 0
    try:
        with _lock:
            _gap()
            r = requests.get(row["url"], headers={"User-Agent": UA}, timeout=90, stream=True)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            content = r.content if len(r.content) <= MAX_PDF_BYTES else None
        if content is None:
            status = "too_big"
        elif MAX_PAGES.get(row["kind"], 30) == 0:
            status = "none"
        else:
            text, pages = _extract(content, row["kind"])
            status = "ok" if len(text.strip()) > 400 else "no_text"      # a scanned PDF has no text layer
    except Exception as e:  # noqa: BLE001
        logger.info("filing %s could not be read: %s", fid, e)
    with get_market_conn() as m:
        if status == "ok":
            m.execute("INSERT OR REPLACE INTO filing_text (id, text, pages, chars, extracted_at) VALUES (?,?,?,?,?)", (fid, text, pages, len(text), _now()))
        m.execute("UPDATE filings SET text_status = ? WHERE id = ?", (status, fid))
        m.commit()
    return status


def latest(symbol: str, kind: str | None = None, n: int = 5, with_text: bool = False) -> list[dict]:
    with get_market_conn() as m:
        sql = "SELECT * FROM filings WHERE symbol = ?" + (" AND kind = ?" if kind else "") + " ORDER BY filed_at DESC LIMIT ?"
        rows = [dict(r) for r in m.execute(sql, (symbol, kind, n) if kind else (symbol, n))]
        if with_text:
            for r in rows:
                t = m.execute("SELECT text FROM filing_text WHERE id = ?", (r["id"],)).fetchone()
                r["text"] = t["text"] if t else None
    return rows


def text_of(fid: str) -> str | None:
    with get_market_conn() as m:
        t = m.execute("SELECT text FROM filing_text WHERE id = ?", (fid,)).fetchone()
    return t["text"] if t else None


def _results_with_numbers(symbol: str) -> dict | None:
    """The newest board-meeting outcome that is a results filing (it carries the results tables and the auditor's note)."""
    rows = latest(symbol, "results", 1)
    return rows[0] if rows else None


def _results_window(filed_at: str) -> bool:
    """Indian companies announce quarterly results in the weeks after a quarter ends: mid-Jan to Feb, Apr to May, mid-Jul to Aug, mid-Oct to Nov."""
    try:
        d = datetime.strptime(filed_at[:10], "%Y-%m-%d")
    except ValueError:
        return False
    return (d.month in (2, 5, 8, 11)) or (d.month in (1, 4, 7, 10) and d.day >= 15)


def _looks_like_transcript(text: str) -> bool:
    """A call transcript is long and has named speakers ("Ajoy Chawla: ..."). A one-page notice that merely mentions a transcript is not one."""
    return len(text) >= 8000 and len(re.findall(r"^[A-Z][A-Za-z.' -]{2,40}:", text, flags=re.M)) >= 4


def _real_transcripts(symbol: str, n: int) -> list[dict]:
    out = []
    for cand in latest(symbol, "transcript", 8):
        if ensure_text(cand["id"]) != "ok":
            continue
        if _looks_like_transcript(text_of(cand["id"]) or ""):
            out.append(cand)
        else:
            with get_market_conn() as m:   # a notice, not a transcript: stop offering it as one
                m.execute("UPDATE filings SET kind = 'other' WHERE id = ?", (cand["id"],))
                m.commit()
        if len(out) >= n:
            break
    return out


def key_documents(symbol: str) -> list[dict]:
    """The filings worth reading for a research report: the latest two call transcripts, the latest investor presentation and the latest results filing.
    When no filing is recognised as results (some companies describe the board outcome in one generic line), the newest board outcomes filed in a results window are
    opened and the first whose text is a results statement is used."""
    docs = _real_transcripts(symbol, 2) + latest(symbol, "presentation", 1)
    res = latest(symbol, "results", 1)
    if res:
        docs.append(res[0])
    else:
        for cand in latest(symbol, "board_outcome", 6):
            if not _results_window(cand["filed_at"]):
                continue
            if ensure_text(cand["id"]) == "ok":
                text = (text_of(cand["id"]) or "").lower()
                if "financial result" in text or ("unaudited" in text and "quarter" in text) or ("audited" in text and "year ended" in text):
                    with get_market_conn() as m:
                        m.execute("UPDATE filings SET kind = 'results' WHERE id = ?", (cand["id"],))
                        m.commit()
                    cand["kind"] = "results"
                    docs.append(cand)
                    break
    return docs


def refresh_key_documents(symbol: str, days: int = 400) -> dict:
    """Sync the list, then read the key documents (skipping any already stored). Returns {"sync", "read": [rows with text_status], "index": [...]}."""
    out = {"sync": sync(symbol, days), "read": []}
    for d in key_documents(symbol):
        d["text_status"] = ensure_text(d["id"])
        out["read"].append(d)
    return out
