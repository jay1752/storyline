"""ETFs and index funds live in their OWN SQLite file (data/funds.db), apart from the stock universe in market.db:
different shape (no quarterly fundamentals; NAV, fees, holdings) and a different refresh rhythm. The app searches both."""

import sqlite3
from pathlib import Path

from app.db import DATA_DIR

FUNDS_DB = DATA_DIR / "funds.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS funds (
    symbol           TEXT PRIMARY KEY,      -- FUND:US:SPY | FUND:NSE:NIFTYBEES | FUND:MF:120716 (AMFI scheme code)
    kind             TEXT NOT NULL,         -- ETF | INDEX_FUND
    market           TEXT NOT NULL,         -- US | IN
    ticker           TEXT,
    name             TEXT NOT NULL,
    isin             TEXT,
    amc              TEXT,                  -- fund house / issuer
    category         TEXT,
    underlying_index TEXT,                  -- e.g. "Nifty 50"
    underlying_asset TEXT,                  -- EQUITY | GOLD | SILVER | DEBT ... (India ETFs) or asset-class summary
    expense_ratio    REAL,                  -- percent per year
    aum              REAL,                  -- in the fund's currency units (USD for US, INR for India where known)
    price            REAL,
    price_date       TEXT,
    nav              REAL,
    nav_date         TEXT,
    scheme_code      TEXT,
    source           TEXT,
    facts_at         TEXT,                  -- when fees/AUM/holdings were last pulled
    updated_at       TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_funds_name ON funds(name);
CREATE INDEX IF NOT EXISTS idx_funds_isin ON funds(isin);

CREATE TABLE IF NOT EXISTS fund_holdings (
    fund            TEXT NOT NULL REFERENCES funds(symbol),
    rank            INTEGER,
    holding_symbol  TEXT,                   -- our stock symbol when we hold it (NSE:RELIANCE, NASDAQ:AAPL), else the raw ticker
    holding_name    TEXT,
    weight_pct      REAL,                   -- NULL when the source gives constituents without weights
    source          TEXT,                   -- 'yahoo top holdings' | 'index constituents (unweighted)'
    PRIMARY KEY (fund, holding_symbol, holding_name)
);

CREATE TABLE IF NOT EXISTS fund_sectors (
    fund      TEXT NOT NULL REFERENCES funds(symbol),
    sector    TEXT NOT NULL,
    weight_pct REAL,
    PRIMARY KEY (fund, sector)
);

CREATE TABLE IF NOT EXISTS fund_prices (    -- market price for ETFs, NAV for index mutual funds
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,
    close  REAL NOT NULL,
    PRIMARY KEY (symbol, date)
);
"""


def get_funds_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(FUNDS_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
