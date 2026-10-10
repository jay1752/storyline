"""SQLite access layer — two files, matching the product's two-layer split.

market.db  -> Analytics layer, shared across every user (stocks, quarterly_data, macro_context, price_history)
users.db   -> Management + Portfolio layer, per user (users, tracked_positions, position_snapshots)

users.db ATTACHes market.db so a position row can be joined straight to its stock's
quarterly_data / price_history without an application-level fan-out query.
"""

import json
import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MARKET_DB = DATA_DIR / "market.db"
USERS_DB = DATA_DIR / "users.db"
CAP_TIERS_FILE = Path(__file__).resolve().parent / "data" / "cap_tiers.json"

MARKET_SCHEMA = """
CREATE TABLE IF NOT EXISTS stocks (
    symbol      TEXT PRIMARY KEY,       -- exchange-qualified, e.g. NSE:TATAMOTORS
    exchange    TEXT NOT NULL,
    name        TEXT NOT NULL,
    sector      TEXT NOT NULL,
    market      TEXT NOT NULL,          -- IN | US
    cap_tier    TEXT                    -- large | mid | small — India only (SEBI/AMFI classification,
                                         -- see app/data/cap_tiers.json); NULL for US and any
                                         -- unclassified India stock, never guessed
);

-- Universe entries removed for having no data at all (no fundamentals, no prices, no 13F rows —
-- warrants/rights/units mostly). Doubles as the backup of what was deleted. The trigger stops
-- the universe syncs (SEC/5paisa/BSE) re-adding them on their next run; to bring one back,
-- delete its row from excluded_stocks and re-run that source's sync_universe.
CREATE TABLE IF NOT EXISTS excluded_stocks (
    symbol      TEXT PRIMARY KEY,
    exchange    TEXT,
    name        TEXT,
    sector      TEXT,
    market      TEXT,
    cap_tier    TEXT,
    excluded_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS stocks_skip_excluded BEFORE INSERT ON stocks
WHEN EXISTS (SELECT 1 FROM excluded_stocks e WHERE e.symbol = NEW.symbol)
BEGIN
    SELECT RAISE(IGNORE);
END;

CREATE TABLE IF NOT EXISTS quarterly_data (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT NOT NULL REFERENCES stocks(symbol),
    period_end_date     TEXT NOT NULL,      -- real calendar date, e.g. "2026-06-30" — the true identity of a quarter
    quarter_index       INTEGER,            -- display-order convenience only; never trust for sorting, ORDER BY period_end_date instead
    quarter_label       TEXT NOT NULL,      -- display string — "Q1".."Q8" for mock, "FY2026 Q3" style for real filings
    source               TEXT NOT NULL DEFAULT 'mock',  -- which ingestion source wrote this row
    revenue_cr          REAL,
    operating_profit_cr REAL,
    opm_pct             REAL,
    other_income_cr     REAL,
    net_profit_cr       REAL,
    eps                 REAL,
    eps_adjusted        REAL,          -- eps, retroactively adjusted for a LATER real bonus/split so
                                        -- the series reads as one continuous trend instead of a cliff.
                                        -- Derived from equity_share_capital_cr's own actual period-to-
                                        -- period ratio (see _backfill_eps_adjustments in this file) —
                                        -- not guessed from the EPS jump itself, which this session
                                        -- confirmed live is usually just real earnings volatility, not
                                        -- a corporate action. NULL unless capital data on both sides of
                                        -- a gap shows a real change; `eps` itself is never overwritten.
    equity_share_capital_cr REAL,      -- paid-up equity share capital, ₹Cr — the real signal a
                                        -- corporate action actually happened (flat = none, changed =
                                        -- one did); confirmed live this session this is a far more
                                        -- reliable discriminator than any EPS-ratio heuristic
    revenue_growth_pct  REAL,
    guidance_vs_delivered TEXT,        -- beat | met | miss
    growth_vs_sector_pp REAL,
    margin_vs_sector_pp REAL,
    fii_pct             REAL,
    dii_pct              REAL,
    promoter_pct         REAL,
    promoter_pledge      TEXT,          -- e.g. "No pledge"
    promoter_pledge_pct  REAL,          -- pledged shares as % of the promoter group's own holding (India, from the shareholding XBRL)
    sector_momentum      TEXT,          -- favor | neutral | out
    total_debt_cr        REAL,          -- NULL except last 4 quarters (matches Details view)
    net_debt_cr          REAL,
    cash_cr               REAL,
    roe_pct               REAL,          -- trailing, populated on latest quarter row only
    roce_pct              REAL,
    debt_equity           REAL,
    pe_current             REAL,
    pe_low_3yr              REAL,
    pe_high_3yr             REAL,
    pbt_cr                 REAL,          -- profit before tax
    tax_pct                REAL,          -- tax expense as % of PBT
    book_value_per_share    REAL,          -- NULL except latest quarter (needs the annual balance sheet, same cadence as debt/cash/roe)
    UNIQUE(symbol, period_end_date)
);

-- Layer 3 (institutional). Doesn't fit quarterly_data's per-filing shape — a 13F
-- row is thousands of managers' positions aggregated UP to one row per symbol per
-- quarter, not a single company's own filing. shares_qoq_change_pct is the real
-- signal (net institutional buying/selling); no pct-of-shares-outstanding here —
-- that would need a 4th data source, deliberately out of scope for v1.
CREATE TABLE IF NOT EXISTS annual_data (
    symbol TEXT NOT NULL,
    period_end_date TEXT NOT NULL,          -- fiscal-year end (India: March)
    source TEXT NOT NULL DEFAULT 'indianapi',
    revenue_cr REAL, expenses_cr REAL, operating_profit_cr REAL, opm_pct REAL, other_income_cr REAL,
    finance_cost_cr REAL, depreciation_cr REAL, pbt_cr REAL, tax_pct REAL, net_profit_cr REAL, eps REAL, dividend_payout_pct REAL,
    equity_capital_cr REAL, reserves_cr REAL, borrowings_cr REAL, other_liabilities_cr REAL, total_liabilities_cr REAL,
    fixed_assets_cr REAL, cwip_cr REAL, investments_cr REAL, other_assets_cr REAL, total_assets_cr REAL,
    cfo_cr REAL, cfi_cr REAL, cff_cr REAL, net_cash_flow_cr REAL, fcf_cr REAL, cfo_to_op REAL,
    debtor_days REAL, inventory_days REAL, days_payable REAL, cash_conversion_cycle REAL, working_capital_days REAL, roce_pct REAL,
    PRIMARY KEY (symbol, period_end_date)
);

CREATE TABLE IF NOT EXISTS stock_metrics (   -- one row per stock with fundamentals: powers the Analytics discovery screens and search context
    symbol TEXT PRIMARY KEY,
    market TEXT, name TEXT, sector TEXT, cap_tier TEXT, bank INTEGER,
    raws TEXT,                -- JSON {rule key: the number that rule is judged on}; judged against the USER's thresholds at request time
    roce REAL, de REAL, growth_min4 REAL, opm REAL, cfo_pat REAL,
    sales_cagr_3y REAL, roce_3y REAL, div_payout REAL,
    pe REAL, pe_low REAL, pe_high REAL, price REAL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS fx_rates (        -- USD/INR (INR per 1 USD) fetched by the hourly fx_usd_inr job; the newest row is the platform rate
    pair TEXT NOT NULL,
    rate REAL NOT NULL,
    source TEXT NOT NULL,         -- frankfurter (ECB) | open.er-api.com | yfinance
    as_of TEXT,                   -- the quote's own date/time as the source reports it
    fetched_at TEXT NOT NULL,     -- UTC
    PRIMARY KEY (pair, fetched_at)
);

CREATE TABLE IF NOT EXISTS results_calendar (   -- announced results / board-meeting dates (NSE + BSE), refreshed daily; drives the post-results data refresh
    symbol     TEXT NOT NULL,                       -- NSE:TCS
    event_date TEXT NOT NULL,                       -- ISO date of the meeting
    purpose    TEXT,                                -- e.g. 'Financial Results/Dividend'
    source     TEXT NOT NULL,                       -- nse | bse
    fetched_at TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'upcoming',    -- upcoming | refreshed | gave_up
    tries      INTEGER NOT NULL DEFAULT 0,          -- post-results refresh attempts so far
    last_try   TEXT,
    note       TEXT,
    PRIMARY KEY (symbol, event_date)
);
CREATE INDEX IF NOT EXISTS idx_results_calendar_date ON results_calendar(event_date, status);

CREATE TABLE IF NOT EXISTS delivery_data (     -- NSE's delivered share of traded volume per stock per day (sec_bhavdata_full), India only
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,
    deliv_qty INTEGER,
    deliv_pct REAL,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS announcements_cache (  -- recent exchange filings per stock (BSE), with IndianAPI's one-line summary where available
    symbol     TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,        -- UTC, last BSE fetch
    ia_at      TEXT,                 -- UTC, last IndianAPI summary fetch (NULL = never)
    items_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ia_usage (          -- calls made to the data vendor per month, so a refresh job can stop before the plan's monthly limit
    month TEXT PRIMARY KEY,                    -- YYYY-MM (UTC)
    calls INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS institutional_holdings (
    symbol                TEXT NOT NULL REFERENCES stocks(symbol),
    period_end_date       TEXT NOT NULL,      -- 13F reporting quarter-end, e.g. "2026-06-30"
    holder_count          INTEGER,            -- distinct 13F filing managers holding this symbol
    total_shares_held     INTEGER,            -- aggregate SSHPRNAMT across all filers
    total_value_usd       INTEGER,            -- aggregate VALUE — confirmed empirically to already be actual USD in this bulk dataset (a *1000 "SEC reports in thousands" assumption was tried and disproven: implied per-share price was ~1000x too high for known large caps)
    shares_qoq_change_pct REAL,               -- vs prior stored quarter; NULL until a second quarter's data exists
    source                 TEXT NOT NULL,
    UNIQUE(symbol, period_end_date)
);

-- Layer 5 (Macro). Real structured numbers, not a narrative blurb — matches how
-- every other layer in this app works. Cadence differs by market and is honest
-- about it: US (FRED) is quarterly-ish, India (World Bank) is annual, no policy
-- rate sourced for India yet — period_end_date is the real date each source
-- reports as-of, not forced onto a shared calendar.
CREATE TABLE IF NOT EXISTS macro_context (
    market            TEXT NOT NULL,
    period_end_date   TEXT NOT NULL,
    policy_rate_pct   REAL,             -- central bank policy rate — US only for now (Fed funds effective rate)
    cpi_yoy_pct       REAL,             -- inflation, year-over-year %
    gdp_growth_pct    REAL,             -- real GDP growth %
    unemployment_pct  REAL,             -- US only (FRED UNRATE) — not tracked for India
    source            TEXT NOT NULL,
    PRIMARY KEY (market, period_end_date)
);

CREATE TABLE IF NOT EXISTS price_history (
    symbol  TEXT NOT NULL REFERENCES stocks(symbol),
    date    TEXT NOT NULL,
    open    REAL, high REAL, low REAL, close REAL, volume INTEGER,
    source  TEXT NOT NULL DEFAULT 'mock',
    PRIMARY KEY (symbol, date)
);

-- Ingestion provenance/audit log — lives on the market.db side because it's about the
-- lifecycle of Analytics-layer data specifically, not a per-user concern.
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name   TEXT NOT NULL,       -- mock | 5paisa | sec_edgar
    target        TEXT NOT NULL,       -- quarterly | prices
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,       -- running | success | failed | not_configured | not_implemented
    rows_written  INTEGER DEFAULT 0,
    error_message TEXT
);
"""

USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT UNIQUE NOT NULL,
    plan       TEXT NOT NULL DEFAULT 'free',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tracked_positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    symbol        TEXT NOT NULL,             -- FK into market.stocks, cross-db (ATTACH)
    company_name  TEXT NOT NULL,
    anchor_type   TEXT NOT NULL,             -- watchlist | position
    anchor_date   TEXT NOT NULL,
    buy_price     REAL,                      -- NULL for watchlist
    quantity      REAL,                      -- NULL for watchlist; shares held, for position-value math
    status        TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS position_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id      INTEGER NOT NULL REFERENCES tracked_positions(id),
    checked_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    price_at_check   REAL,
    since_anchor_pct REAL,
    one_yr_pct       REAL,
    valuation_label  TEXT,
    signal           TEXT,                    -- hold | trim_consider | watch | exit_consider
    reasoning_json   TEXT,                     -- JSON array of short bullet strings
    trigger_type     TEXT                      -- interval | event
);

-- The user's OWN journal for a tracked stock — fully optional. Each entry changes the position when it is made
-- (add: weighted-average cost; trim: fewer shares; exit: closed) and records `before_json` so the most recent entry
-- can be undone exactly. The daily analysis reads these entries and judges them against the user's framework.
CREATE TABLE IF NOT EXISTS position_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id  INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    event_type   TEXT NOT NULL,                 -- open | add | trim | exit | note   ('open' = baseline written with the first entry)
    event_date   TEXT NOT NULL,                 -- the date the user says it happened (YYYY-MM-DD)
    quantity     REAL,
    price        REAL,
    note         TEXT,
    before_json  TEXT,                          -- tracked_positions fields as they were before this entry, for undo
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_position_events_pos ON position_events(position_id, event_date, id);

-- One row per attempt to analyse a tracked stock (manual button or the daily job). The analysis itself lands in
-- position_snapshots; this table exists so a FAILED or still-running attempt is visible in the UI instead of silent.
CREATE TABLE IF NOT EXISTS insight_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id  INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    trigger      TEXT NOT NULL DEFAULT 'manual',          -- manual | daily
    status       TEXT NOT NULL DEFAULT 'queued',          -- queued | running | success | failed
    error        TEXT,
    started_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_insight_runs_pos ON insight_runs(position_id, id);

-- Admin overrides for the in-app scheduler's jobs (time/days/on-off). A row exists only where an admin changed
-- something; deleting it restores the defaults in app/scheduler.py JOBS.
CREATE TABLE IF NOT EXISTS job_settings (
    key         TEXT PRIMARY KEY,
    enabled     INTEGER NOT NULL DEFAULT 1,
    hm          INTEGER NOT NULL,                -- minute of day, IST
    days        TEXT NOT NULL,                   -- e.g. "0,1,2,3,4" (Mon=0)
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- The investment-rule thresholds behind the Fundamentals scan (ROCE > 25 ...). Three layers over the built-in
-- defaults in app/framework.py: platform (admin) < user < stock. A row only exists where something was CHANGED;
-- deleting it is "reset" (the value then comes from the layer below).
CREATE TABLE IF NOT EXISTS framework_overrides (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT NOT NULL CHECK (scope IN ('platform', 'user', 'stock')),
    user_id     INTEGER NOT NULL DEFAULT 0,      -- 0 for the platform layer
    symbol      TEXT NOT NULL DEFAULT '',        -- '' unless scope = 'stock'
    key         TEXT NOT NULL,
    value       REAL NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (scope, user_id, symbol, key)
);

-- BYOK settings (AI chat + Insights). One row per user, created on first /settings POST
-- (see app/ai/providers.py's get_user_settings for the "no row yet" default).
CREATE TABLE IF NOT EXISTS user_settings (
    user_id               INTEGER PRIMARY KEY REFERENCES users(id),
    provider              TEXT NOT NULL DEFAULT 'gemini',
    model_name            TEXT NOT NULL DEFAULT 'gemini-2.5-flash',
    use_platform_default  INTEGER NOT NULL DEFAULT 1,     -- 0/1 bool; NULL encrypted_api_key whenever this is 1
    encrypted_api_key     TEXT,                            -- Fernet ciphertext (app/ai/crypto.py), never plaintext
    updated_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- One BYOK key PER PROVIDER (a user can keep a Claude key, a Groq key, a Gemini key... at once and switch
-- between them in AI settings). Fernet ciphertext, never plaintext. Supersedes user_settings.encrypted_api_key,
-- which is kept only as the source for the one-time migration in init_db().
CREATE TABLE IF NOT EXISTS user_provider_keys (
    user_id            INTEGER NOT NULL REFERENCES users(id),
    provider           TEXT NOT NULL,
    encrypted_api_key  TEXT NOT NULL,
    updated_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, provider)
);

-- Community: one shared channel. A row with parent_id NULL is a top-level message; replies hang off it
-- (one level deep, Slack-thread style). user_id NULL = posted by the system (the daily AI briefing).
-- briefing_date is set only on AI briefings and is UNIQUE, so the daily job can never post twice for a day.
-- Timestamps are UTC (CURRENT_TIMESTAMP); the API sends them as ISO-8601 'Z' for the browser to localise.
CREATE TABLE IF NOT EXISTS community_posts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER REFERENCES users(id),
    kind           TEXT NOT NULL DEFAULT 'user',          -- 'user' | 'ai_daily'
    body           TEXT NOT NULL,
    parent_id      INTEGER REFERENCES community_posts(id),
    symbol         TEXT,                                  -- first stock tagged with $TICKER, e.g. NSE:MCX
    briefing_date  TEXT,
    meta_json      TEXT,
    deleted        INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_community_parent ON community_posts(parent_id, id);
CREATE INDEX IF NOT EXISTS idx_community_symbol ON community_posts(symbol, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_community_briefing ON community_posts(briefing_date) WHERE briefing_date IS NOT NULL;

CREATE TABLE IF NOT EXISTS community_reactions (
    post_id   INTEGER NOT NULL REFERENCES community_posts(id),
    user_id   INTEGER NOT NULL REFERENCES users(id),
    emoji     TEXT NOT NULL,
    PRIMARY KEY (post_id, user_id, emoji)
);

-- Statement imports proposed by the chat assistant, awaiting the user's explicit
-- confirm click (app/positions.py). payload_json holds the computed positions; nothing
-- reaches tracked_positions until status flips to 'confirmed' via the user-click endpoint.
CREATE TABLE IF NOT EXISTS pending_imports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending | confirmed | cancelled | superseded
    payload_json TEXT NOT NULL
);

-- Server-side sessions (app/auth/tokens.py). session_id is a high-entropy random
-- string used as an opaque bearer credential in an HttpOnly cookie — not signed/JWT,
-- so logout/revocation is a plain DELETE, not a blocklist.
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL
);

-- Single-use expiring tokens, shared by email verification and password reset
-- (app/auth/tokens.py) — structurally identical (random, expiring, single-use),
-- distinguished only by `purpose`.
CREATE TABLE IF NOT EXISTS auth_tokens (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    purpose    TEXT NOT NULL,     -- verify_email | reset_password
    expires_at TEXT NOT NULL,
    used_at    TEXT
);
"""


def _backfill_cap_tiers(conn: sqlite3.Connection) -> None:
    """Idempotent — safe to run on every startup. Only ever sets cap_tier for a
    symbol this file actually classifies (SEBI/AMFI Large/Mid/Small, India only);
    never touches a symbol that isn't in it, so a stock this file doesn't cover
    stays NULL rather than getting a guessed tier."""
    if not CAP_TIERS_FILE.exists():
        return
    with open(CAP_TIERS_FILE) as f:
        tiers = json.load(f)
    conn.executemany(
        "UPDATE stocks SET cap_tier = ? WHERE symbol = ? AND (cap_tier IS NULL OR cap_tier != ?)",
        [(tier, symbol, tier) for symbol, tier in tiers.items()],
    )


_MIN_CAPITAL_RATIO_CHANGE = 0.06  # deviation from 1.0 beyond which a capital change
# is treated as a real corporate action rather than rounding noise / a trivial ESOP issuance


def _backfill_eps_adjustments_from(conn: sqlite3.Connection, capital_column: str) -> None:
    """Shared by both India (equity_share_capital_cr, ₹Cr paid-up capital) and US
    (shares_outstanding, raw share count) — same logic, different signal for "did a
    real corporate action happen": a real, non-trivial change in either one is the
    same kind of event (bonus/split/rights issue for India; stock split for US),
    and the ratio of the change is the correct EPS adjustment factor either way.
    See _backfill_eps_adjustments's own docstring for why this replaced an earlier
    hand-researched registry. `eps` itself is never modified."""
    candidate_symbols = [r[0] for r in conn.execute(
        f"SELECT symbol FROM quarterly_data WHERE {capital_column} IS NOT NULL GROUP BY symbol HAVING COUNT(*) >= 2"
    ).fetchall()]

    for symbol in candidate_symbols:
        rows = conn.execute(
            f"SELECT period_end_date, eps, {capital_column} AS capital FROM quarterly_data "
            "WHERE symbol = ? ORDER BY period_end_date",
            (symbol,),
        ).fetchall()

        capital_points = [(r["period_end_date"], r["capital"]) for r in rows if r["capital"] is not None]
        events = []  # (period_end_date of the later period, ratio)
        for (d1, c1), (d2, c2) in zip(capital_points, capital_points[1:]):
            if c1 and c2 and c1 > 0:
                ratio = c2 / c1
                if abs(ratio - 1.0) > _MIN_CAPITAL_RATIO_CHANGE:
                    events.append((d2, ratio))
        if not events:
            continue

        updates = []
        for r in rows:
            if r["eps"] is None:
                continue
            cumulative = 1.0
            for ev_date, ratio in events:
                if r["period_end_date"] < ev_date:
                    cumulative *= ratio
            if cumulative != 1.0:
                updates.append((round(r["eps"] / cumulative, 4), symbol, r["period_end_date"]))
        if updates:
            conn.executemany(
                "UPDATE quarterly_data SET eps_adjusted = ? WHERE symbol = ? AND period_end_date = ?",
                updates,
            )


def _backfill_eps_adjustments(conn: sqlite3.Connection) -> None:
    """Idempotent — safe to run on every startup/re-ingestion. Real gap found live
    this session: an earlier version of this function used a small hand-researched
    registry (corporate_actions.json — 3 symbols, verified one at a time against
    Screener.in and news search) after a systematic scan across just large-cap
    stocks found 26 "looks like a bonus/split" EPS-ratio candidates, and checking a
    sample of them showed MOST were genuine earnings volatility (real bank-sector
    NPA-cycle recovery, real commodity-cycle swings), not corporate actions at all
    — guessing from the EPS jump alone is unreliable. A symbol's own share count
    (equity_share_capital_cr for India, shares_outstanding for US) is the signal
    that was right every time it was checked: flat = no corporate action, however
    dramatic the EPS swing; changed = a real bonus/split/rights issue, and the
    ratio of the change IS the correct adjustment factor — no need to guess
    whether it was "1:1" or "1:3". Confirmed live on Apple: shares_outstanding
    shows a smooth quarter-to-quarter decline (its real, ongoing buyback program),
    correctly NOT flagged as an event — only a genuine step change would be."""
    _backfill_eps_adjustments_from(conn, "equity_share_capital_cr")
    _backfill_eps_adjustments_from(conn, "shares_outstanding")


def _connect(path: Path) -> sqlite3.Connection:
    # Deliberately NOT WAL mode. journal_mode is a property of the database FILE, not
    # the connection — once one connection flips it to WAL, every later connection uses
    # WAL too, whether it asks for it or not. WAL's shared-memory (-shm) file needs
    # locking/mmap support that Docker Desktop's bind-mounted volumes don't reliably give
    # on macOS: concurrent background tasks hit a real "disk I/O error" opening it (seen
    # directly in this session). The default rollback journal works everywhere; at this
    # app's scale (single worker, rarely more than one background task at a time) the
    # concurrent-read win from WAL isn't worth that fragility.
    # timeout=60 (was 30): raised after real "database is locked" failures under 7-way
    # concurrent ingestion this session — extra headroom alongside walking
    # MAX_CONCURRENT_JOBS back down to 4 (see runner.py).
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def get_market_conn() -> sqlite3.Connection:
    return _connect(MARKET_DB)


def get_users_conn() -> sqlite3.Connection:
    """users.db with market.db ATTACHed as `market` for cross-file joins."""
    conn = _connect(USERS_DB)
    conn.execute("ATTACH DATABASE ? AS market", (str(MARKET_DB),))
    return conn


def _guess_provider_from_key(key: str, fallback: str) -> str:
    """Which provider a pasted key belongs to, from its well-known prefix (used once, to move the old
    single-key setup into per-provider storage). Ambiguous keys stay with the provider they were saved under."""
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key.startswith(("AIza", "AQ.")):
        return "gemini"
    if key.startswith("gsk_"):
        return "groq"
    if key.startswith("sk-or-"):
        return "openrouter"
    if key.startswith("xai-"):
        return "xai"
    return fallback


def _migrate_legacy_api_keys(u) -> None:
    """One-time: user_settings.encrypted_api_key (a single key tied to whatever provider was selected)
    -> a row in user_provider_keys. Idempotent; silently skipped if the master key isn't available."""
    try:
        from app.ai.crypto import decrypt
    except Exception:  # noqa: BLE001
        return
    for row in u.execute(
        "SELECT user_id, provider, encrypted_api_key FROM user_settings WHERE encrypted_api_key IS NOT NULL"
    ).fetchall():
        if u.execute("SELECT 1 FROM user_provider_keys WHERE user_id = ?", (row["user_id"],)).fetchone():
            continue
        try:
            provider = _guess_provider_from_key(decrypt(row["encrypted_api_key"]), row["provider"])
        except Exception:  # noqa: BLE001 — undecryptable (rotated master key): leave it alone
            continue
        u.execute(
            "INSERT OR IGNORE INTO user_provider_keys (user_id, provider, encrypted_api_key) VALUES (?,?,?)",
            (row["user_id"], provider, row["encrypted_api_key"]),
        )


def _ensure_platform_admin(u) -> None:
    """Guarantee someone can reach Admin: if NO user has the flag, promote the first account (the owner who set the
    app up). Once any admin exists this does nothing, so it can never grant admin to a later sign-up."""
    if u.execute("SELECT 1 FROM users WHERE is_platform_admin = 1 LIMIT 1").fetchone():
        return
    u.execute("UPDATE users SET is_platform_admin = 1 WHERE id = (SELECT MIN(id) FROM users)")


def ensure_platform_admin() -> None:
    """Same guarantee, callable after the first-run seed creates the owner account."""
    with get_users_conn() as u:
        _ensure_platform_admin(u)
        u.commit()


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _connect(MARKET_DB) as m:
        # One-time migration: macro_context's shape changed (quarter_label/note ->
        # structured columns) after the table already existed live — CREATE TABLE IF
        # NOT EXISTS doesn't alter an existing table, so the old shape would otherwise
        # stick around forever. Safe to drop unconditionally: this table was a phase-2
        # stub that never held real data (confirmed — always empty until this session).
        cols = {row["name"] for row in m.execute("PRAGMA table_info(macro_context)").fetchall()}
        if cols and "period_end_date" not in cols:
            m.execute("DROP TABLE macro_context")
        m.executescript(MARKET_SCHEMA)

        # Additive migration: quarterly_data already holds real, valuable data (unlike
        # macro_context above), so new columns get ALTER TABLE ADD COLUMN, never a
        # drop-and-recreate. Idempotent — only adds what's actually missing.
        qd_cols = {row["name"] for row in m.execute("PRAGMA table_info(quarterly_data)").fetchall()}
        for col, decl in [("pbt_cr", "REAL"), ("tax_pct", "REAL"), ("book_value_per_share", "REAL"), ("shares_outstanding", "REAL")]:
            if col not in qd_cols:
                m.execute(f"ALTER TABLE quarterly_data ADD COLUMN {col} {decl}")
        if "eps_adjusted" not in qd_cols:
            m.execute("ALTER TABLE quarterly_data ADD COLUMN eps_adjusted REAL")
        if "equity_share_capital_cr" not in qd_cols:
            m.execute("ALTER TABLE quarterly_data ADD COLUMN equity_share_capital_cr REAL")
        # Inputs for the scan's interest-coverage / current / quick / CFO-to-PAT rows (₹ Cr for India, $ M for US).
        for col in ("finance_cost_cr", "current_assets_cr", "current_liabilities_cr", "inventory_cr", "cfo_cr"):
            if col not in qd_cols:
                m.execute(f"ALTER TABLE quarterly_data ADD COLUMN {col} REAL")
        if "promoter_pledge_pct" not in qd_cols:
            m.execute("ALTER TABLE quarterly_data ADD COLUMN promoter_pledge_pct REAL")
        stocks_cols = {row["name"] for row in m.execute("PRAGMA table_info(stocks)").fetchall()}
        if "isin" not in stocks_cols:
            m.execute("ALTER TABLE stocks ADD COLUMN isin TEXT")  # authoritative security id on broker statements (India)
        m.execute("CREATE INDEX IF NOT EXISTS idx_stocks_isin ON stocks(isin) WHERE isin IS NOT NULL")
        if "cap_tier" not in stocks_cols:
            m.execute("ALTER TABLE stocks ADD COLUMN cap_tier TEXT")
        _backfill_cap_tiers(m)
        _backfill_eps_adjustments(m)
        # Defensive: if this file was ever left in WAL mode (e.g. from before this fix),
        # convert it back — a no-op if it's already the default rollback journal.
        m.execute("PRAGMA journal_mode=DELETE;")
    with _connect(USERS_DB) as u:
        u.executescript(USERS_SCHEMA)
        # Additive migration: position_snapshots already holds real rows (the
        # placeholder "first loop check pending" row written at tracking time), so new
        # columns get ALTER TABLE ADD COLUMN, same discipline as quarterly_data above —
        # for the daily Insights job's structured checklist result (evidence_json) and a
        # short record of what news fed into that day's narrative (news_summary).
        ps_cols = {row["name"] for row in u.execute("PRAGMA table_info(position_snapshots)").fetchall()}
        for col, decl in [("evidence_json", "TEXT"), ("news_summary", "TEXT"), ("analysis_json", "TEXT")]:
            if col not in ps_cols:
                u.execute(f"ALTER TABLE position_snapshots ADD COLUMN {col} {decl}")
        # Additive migration: users already holds the seeded real account — password_hash
        # NULL means "exists but never set a password" (the auth signup flow's "claim
        # this row" case, see app/routes/auth.py), not "broken."
        users_cols = {row["name"] for row in u.execute("PRAGMA table_info(users)").fetchall()}
        # is_platform_admin: 1 = may see and use the Admin section (data ingestion pipeline, jobs). Default 0 for
        # everyone; granted explicitly (scripts/set_platform_admin.sh) — never by signing up.
        for col, decl in [("password_hash", "TEXT"), ("email_verified_at", "TEXT"), ("is_platform_admin", "INTEGER NOT NULL DEFAULT 0")]:
            if col not in users_cols:
                u.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")
        if "display_name" not in {row["name"] for row in u.execute("PRAGMA table_info(users)").fetchall()}:
            u.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
        have = {row["name"] for row in u.execute("PRAGMA table_info(users)").fetchall()}
        for col in ("phone", "contact_email", "country", "city", "about"):  # the Profile page
            if col not in have:
                u.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
        if "google_sub" not in {row["name"] for row in u.execute("PRAGMA table_info(users)").fetchall()}:
            u.execute("ALTER TABLE users ADD COLUMN google_sub TEXT")  # Google account id (OIDC `sub`) for "Sign in with Google"
        if "welcomed_at" not in {row["name"] for row in u.execute("PRAGMA table_info(users)").fetchall()}:
            u.execute("ALTER TABLE users ADD COLUMN welcomed_at TEXT")  # first visit to the /welcome getting-started page
        u.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_sub ON users(google_sub) WHERE google_sub IS NOT NULL")
        us_cols = {row["name"] for row in u.execute("PRAGMA table_info(user_settings)").fetchall()}
        for col in ("fallback_provider", "fallback_model"):
            if col not in us_cols:
                u.execute(f"ALTER TABLE user_settings ADD COLUMN {col} TEXT")
        tp_cols = {row["name"] for row in u.execute("PRAGMA table_info(tracked_positions)").fetchall()}
        for col, decl in [("closed_at", "TEXT"), ("exit_price", "REAL")]:
            if col not in tp_cols:
                u.execute(f"ALTER TABLE tracked_positions ADD COLUMN {col} {decl}")
        if "auto_insights" not in us_cols:  # daily automatic analysis of the user's tracked stocks: on unless switched off
            u.execute("ALTER TABLE user_settings ADD COLUMN auto_insights INTEGER NOT NULL DEFAULT 1")
        _migrate_legacy_api_keys(u)
        _ensure_platform_admin(u)
        u.execute("PRAGMA journal_mode=DELETE;")
