"""IndianAPI data is staged in its OWN SQLite file (data/indianapi.db), apart from market.db.

Why separate: it is third-party, unlicensed-for-display data from a vendor that doesn't disclose its sources, in different
definitions from our filings (e.g. 'Sales' != RevenueFromOperations, consolidated/standalone unlabelled). Keeping it apart means
it can never silently overwrite filing-derived values, every row is traceable to the raw JSON under data/indianapi/raw, and the
whole thing can be dropped without touching market.db. A later, explicit step decides which gaps in market.db it may fill."""

import sqlite3

from app.db import DATA_DIR

INDIANAPI_DB = DATA_DIR / "indianapi.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ia_stock (
    symbol       TEXT PRIMARY KEY,          -- NSE:TCS
    ia_name      TEXT,
    layout       TEXT,                      -- 'standard' | 'bank' (bank/NBFC statements use Revenue/Financing Profit rows)
    status       TEXT NOT NULL,             -- 'loaded' (identity verified) | 'unchecked' (too few comparable quarters) | 'held_out' (disagrees with BSE)
    identity_ok  INTEGER,                   -- 1 / 0 / NULL
    has_profile  INTEGER DEFAULT 0,
    loaded_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ia_fact (        -- statements + ratios, long format; values in INR Cr unless the metric is a ratio / per-share / %
    symbol     TEXT NOT NULL,
    period_end TEXT NOT NULL,               -- ISO month end
    freq       TEXT NOT NULL,               -- 'A' annual (fiscal year) | 'Q' quarter
    statement  TEXT NOT NULL,               -- PL | BS | CF | RATIO
    metric     TEXT NOT NULL,               -- IndianAPI's own row label, unchanged
    value      REAL,
    PRIMARY KEY (symbol, period_end, freq, metric)
);
CREATE INDEX IF NOT EXISTS idx_ia_fact_metric ON ia_fact(metric, freq);

CREATE TABLE IF NOT EXISTS ia_holding (     -- shareholding pattern, percent
    symbol     TEXT NOT NULL,
    period_end TEXT NOT NULL,
    freq       TEXT NOT NULL,               -- 'Q' | 'A'
    category   TEXT NOT NULL,               -- Promoters | FIIs | DIIs | Government | Public | No. of Shareholders
    value      REAL,
    PRIMARY KEY (symbol, period_end, freq, category)
);

CREATE TABLE IF NOT EXISTS ia_growth (      -- compounded growth / CAGR / ROE summary
    symbol  TEXT NOT NULL,
    metric  TEXT NOT NULL,
    horizon TEXT NOT NULL,                  -- 10Y | 5Y | 3Y | 1Y | TTM | Last Year
    pct     REAL,
    PRIMARY KEY (symbol, metric, horizon)
);

CREATE TABLE IF NOT EXISTS ia_target (      -- analyst price target consensus
    symbol TEXT PRIMARY KEY,
    mean REAL, high REAL, low REAL, median REAL, n_estimates INTEGER, std REAL, currency TEXT,
    recommendation_json TEXT,
    fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS ia_target_snapshot (
    symbol TEXT NOT NULL, age TEXT NOT NULL,   -- OneWeekAgo | ThirtyDaysAgo | SixtyDaysAgo | NinetyDaysAgo
    mean REAL, high REAL, low REAL, median REAL, n_estimates INTEGER,
    PRIMARY KEY (symbol, age)
);

CREATE TABLE IF NOT EXISTS ia_estimate (    -- consensus estimates and reported actuals on the ANALYST basis (ex one-offs; not filed EPS)
    symbol      TEXT NOT NULL,
    measure     TEXT NOT NULL,              -- EPS | SAL | NET
    freq        TEXT NOT NULL,              -- Annual | Interim
    fiscal_type TEXT NOT NULL,              -- FY | Q1..Q4 (as IndianAPI labels them)
    fiscal_year INTEGER NOT NULL,
    kind        TEXT NOT NULL,              -- 'estimate' | 'actual'
    mean REAL, high REAL, low REAL, median REAL, n_estimates INTEGER, std REAL,
    reported REAL, reported_date TEXT, surprise_pct REAL,
    PRIMARY KEY (symbol, measure, freq, fiscal_type, fiscal_year, kind)
);

CREATE TABLE IF NOT EXISTS ia_stmt (        -- the second, Refinitiv-style statement set from /stock (INR millions; analyst-normalised line items)
    symbol TEXT NOT NULL, period_end TEXT NOT NULL,
    freq TEXT NOT NULL,                     -- 'A' annual | 'I' interim (quarter / half)
    statement TEXT NOT NULL,                -- INC | BAL | CAS
    key TEXT NOT NULL, label TEXT, value REAL,
    PRIMARY KEY (symbol, period_end, freq, statement, key)
);

CREATE TABLE IF NOT EXISTS ia_peer (
    symbol TEXT NOT NULL, peer_name TEXT NOT NULL, peer_ticker_id TEXT,
    pe REAL, pb REAL, market_cap REAL, price REAL, pct_change REAL, rating TEXT,
    PRIMARY KEY (symbol, peer_name)
);

CREATE TABLE IF NOT EXISTS ia_quote (       -- snapshot at fetch time (our own price history stays the price source of truth)
    symbol TEXT PRIMARY KEY, nse REAL, bse REAL, pct_change REAL, year_high REAL, year_low REAL, fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS ia_technical (   -- moving-average levels
    symbol TEXT NOT NULL, days INTEGER NOT NULL, nse_price REAL, bse_price REAL,
    PRIMARY KEY (symbol, days)
);

CREATE TABLE IF NOT EXISTS ia_profile (
    symbol TEXT PRIMARY KEY,
    description TEXT, industry TEXT, isin TEXT, nse_code TEXT, bse_code TEXT,
    price REAL, price_date TEXT, risk_category TEXT, risk_std REAL,
    officers_json TEXT, peers_json TEXT, key_metrics_json TEXT, analyst_view_json TEXT, recos_json TEXT,
    corporate_actions_json TEXT, news_json TEXT
);
"""


def get_indianapi_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(INDIANAPI_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
