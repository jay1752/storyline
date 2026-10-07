"""Shared logic for creating a tracked position — used by the quick one-click /track
action (Analytics page "Track" button), the fuller /management/add form, and the chat
statement-import flow (app/positions.py). Kept in one place so all three entry points write tracked_positions the same
way (same placeholder snapshot shape, same defaults)."""

import json
import re
from datetime import date


def create_tracked_position(conn, user_id, symbol, company_name, anchor_type, anchor_date=None, buy_price=None, quantity=None):
    """Inserts a tracked_positions row + its initial (not-yet-assessed) snapshot.
    Caller commits. Returns the new position id."""
    anchor_date = anchor_date or date.today().isoformat()
    cur = conn.execute(
        """INSERT INTO tracked_positions
        (user_id, symbol, company_name, anchor_type, anchor_date, buy_price, quantity, status)
        VALUES (?,?,?,?,?,?,?,'active')""",
        (user_id, symbol, company_name, anchor_type, anchor_date, buy_price, quantity),
    )
    position_id = cur.lastrowid
    conn.execute(
        """INSERT INTO position_snapshots
        (position_id, price_at_check, since_anchor_pct, one_yr_pct, valuation_label, signal, reasoning_json, trigger_type)
        VALUES (?,?,?,?,?,?,?,?)""",
        (position_id, buy_price, 0.0, 0.0, "Not yet assessed", "watch",
         json.dumps(["Just added — first loop check pending"]), "interval"),
    )
    return position_id


def already_tracked(conn, user_id, symbol) -> bool:
    return conn.execute(
        "SELECT 1 FROM tracked_positions WHERE user_id = ? AND symbol = ? AND status = 'active'",
        (user_id, symbol),
    ).fetchone() is not None


def nearest_price_on_or_before(conn, symbol, target_date):
    """Latest close on/before target_date — the honest "price as of that date" a
    buy on that day would have been near. Falls back to the nearest date AFTER it
    if we have no history before (e.g. the chosen date predates our ingested price
    history), so the field still has a sane starting suggestion rather than a blank.
    Returns (price, actual_date_used) or (None, None) if this symbol has no price
    history at all."""
    row = conn.execute(
        "SELECT close, date FROM price_history WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 1",
        (symbol, target_date),
    ).fetchone()
    if row:
        return row["close"], row["date"]
    row = conn.execute(
        "SELECT close, date FROM price_history WHERE symbol = ? AND date > ? ORDER BY date ASC LIMIT 1",
        (symbol, target_date),
    ).fetchone()
    if row:
        return row["close"], row["date"]
    return None, None


def build_symbol_lookup(conn):
    """{bare ticker or full symbol (both UPPER) -> canonical 'EXCH:TICKER' symbol},
    plus {canonical symbol -> display name} — used to match a broker statement's
    printed scrip name/ticker against our own stocks table. Bare-ticker keys let a
    statement that just says "RELIANCE" resolve to NSE:RELIANCE without the user
    having to know our internal symbol format.

    Real bug found live: a bare ticker can collide across markets (NSE:FACT — a real
    Indian fertilizer company — vs. NASDAQ:FACT, a US SPAC; NSE:LTM vs. NASDAQ:LTM/
    LATAM Airlines) and whichever happened to be inserted first silently won,
    regardless of which one a real Indian broker statement almost certainly meant.
    Rows are now sorted NSE-first before the lookup is built, matching the same
    NSE-preferred convention fivepaisa_source.py already uses for dual-listed
    stocks — this is an India-first product, and a broker statement upload is
    overwhelmingly more likely to hold NSE stocks than an identically-ticker'd,
    unrelated US micro-cap."""
    lookup, names = {}, {}
    rows = conn.execute("SELECT symbol, name FROM stocks").fetchall()
    rows = sorted(rows, key=lambda r: 0 if r["symbol"].startswith("NSE:") else 1)
    for row in rows:
        sym = row["symbol"]
        names[sym] = row["name"]
        lookup[sym.upper()] = sym
        bare = sym.split(":", 1)[-1].upper()
        lookup.setdefault(bare, sym)
    return lookup, names


_WORD_RE = re.compile(r"[A-Z]{3,}")
# Generic corporate-suffix words — nearly every company name ends in one of these, so
# they carry ~zero identifying signal and were producing false-positive matches (a
# real one caught live: an Indian company's name overlapping with "Gold.com, Inc." on
# nothing but the word "LIMITED"/shared generic terms). Stripped before scoring.
_STOPWORDS = {
    "LIMITED", "LTD", "LTDA", "INC", "INCORPORATED", "CORP", "CORPORATION",
    "COMPANY", "CO", "THE", "AND", "PVT", "PRIVATE", "PLC", "HOLDINGS", "GROUP",
}


def resolve_symbol(company_text, lookup, names):
    """Best-effort match of a statement's printed scrip text against our stocks table.
    Returns (symbol, candidates): `symbol` is set only for a UNIQUE confident match;
    when several stocks tie, `symbol` is None and `candidates` lists them so the caller
    can ask instead of guessing. Tried in order: exact ticker/symbol match, ticker
    embedded in a longer string (e.g. "RELIANCE-EQ" / "TATAMOTORS BSE"), exact company
    name, then word-overlap against the registered name.

    Real bug found live: "TATA MOTORS LTD" tied at ratio 1.0 between NSE:TMCV ("Tata
    Motors Limited") and NSE:TATAMOTORS ("TATA MOTORS LIMITED") — the old version kept
    whichever came first in the table and returned it as if certain, which was fine
    when a human reviewed every row but silently wrong behind an AI summary + confirm
    card. A tie is now reported as ambiguous, never resolved by table order."""
    text = (company_text or "").strip().upper()
    if not text:
        return None, []
    if text in lookup:
        return lookup[text], []
    token = re.split(r"[\-\s]", text)[0]
    if token in lookup:
        return lookup[token], []
    exact = _prefer_nse([sym for sym, name in names.items() if name.upper() == text])
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, exact[:5]

    # Word-overlap fallback, scored as a ratio (not a raw count) so a company whose
    # name is a superset of another's — e.g. "TATA MOTORS LIMITED" vs "TATA MOTORS
    # PASSENGER VEHICLES LIMITED" — doesn't tie with the actual exact match. Generic
    # corporate-suffix words are stripped first (see _STOPWORDS) so they can't carry
    # a match on their own. A real, confident-looking wrong match is worse than no
    # match, so this is deliberately conservative: it requires both a high ratio AND
    # at least 2 real overlapping words.
    text_words = set(_WORD_RE.findall(text)) - _STOPWORDS
    if not text_words:
        return None, []
    scored = []
    for sym, name in names.items():
        name_words = set(_WORD_RE.findall(name.upper())) - _STOPWORDS
        if not name_words:
            continue
        overlap = len(text_words & name_words)
        if overlap < 2:
            continue
        scored.append((overlap / max(len(text_words), len(name_words)), sym))
    if not scored:
        return None, []
    best_ratio = max(r for r, _ in scored)
    if best_ratio < 0.6:
        return None, []
    top = _prefer_nse([sym for r, sym in scored if r == best_ratio])
    if len(top) == 1:
        return top[0], []
    return None, top[:5]


def _prefer_nse(symbols):
    """This is an India-first product: when a name matches both an NSE listing and an
    identically-named US one, the NSE one is overwhelmingly what a broker statement
    meant (same convention build_symbol_lookup already uses for bare tickers)."""
    nse = [s for s in symbols if s.startswith("NSE:")]
    return nse or list(symbols)
