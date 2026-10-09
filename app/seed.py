"""Dev bootstrap: market data now comes from the ingestion layer (MockSource); this
script only seeds user-side data — one mock user and their tracked positions — which
isn't "ingestion" in the market-data sense, it's just dev fixture data.
"""

import json

from app.db import get_market_conn, get_users_conn, init_db
from app.ingestion.mock_data import STOCKS
from app.ingestion.mock_source import MockSource

USER_EMAIL = "kumarjaydeep1752@gmail.com"

# quantity chosen so USD-equivalent value hits the target book weights (32/26/24/18%)
# used in the Position Desk mockup: NVDA 1127sh, HDFCBANK 6062sh, TATAMOTORS 11166sh, AAPL 462sh.
POSITIONS = [
    dict(symbol="NSE:TATAMOTORS", company_name="Tata Motors Ltd.", anchor_type="position",
         anchor_date="2025-11-14", buy_price=780, quantity=11166, price_now=892,
         since_anchor_pct=14.4, one_yr_pct=22, valuation_label="Upper-mid range",
         signal="hold", trigger_type="interval",
         reasoning=["Margin trend intact — 3rd straight quarter of expansion",
                    "Guidance beat streak continues"]),
    dict(symbol="NASDAQ:NVDA", company_name="NVIDIA Corp.", anchor_type="position",
         anchor_date="2025-08-02", buy_price=108, quantity=1127, price_now=142,
         since_anchor_pct=31.5, one_yr_pct=64, valuation_label="Near top of range",
         signal="trim_consider", trigger_type="event",
         reasoning=["Position weight has grown to 32% of book from price appreciation alone, not a new decision to add.",
                    "P/E at 41.2x sits in the top decile of its 3-year range (18.4x-44.0x).",
                    "Q3 earnings beat (2 days ago) confirms fundamentals - this is a sizing/valuation flag, not a thesis break."]),
    dict(symbol="NSE:HDFCBANK", company_name="HDFC Bank Ltd.", anchor_type="position",
         anchor_date="2025-09-01", buy_price=1650, quantity=6062, price_now=1780,
         since_anchor_pct=7.9, one_yr_pct=14, valuation_label="Fair",
         signal="hold", trigger_type="interval",
         reasoning=["Steady NII growth, asset quality stable"]),
    dict(symbol="NASDAQ:AAPL", company_name="Apple Inc.", anchor_type="position",
         anchor_date="2025-10-01", buy_price=185, quantity=462, price_now=195,
         since_anchor_pct=5.5, one_yr_pct=9, valuation_label="Fair - mid range",
         signal="watch", trigger_type="interval",
         reasoning=["Modest quarter, no thesis change - QTD contribution slightly negative"]),
    dict(symbol="NSE:INFY", company_name="Infosys Ltd.", anchor_type="watchlist",
         anchor_date="2026-06-01", buy_price=None, quantity=None, price_now=None,
         since_anchor_pct=6.2, one_yr_pct=18, valuation_label="Fair - mid range",
         signal="watch", trigger_type="interval",
         reasoning=["No material change since added - steady state"]),
    dict(symbol="NASDAQ:MSFT", company_name="Microsoft Corp.", anchor_type="watchlist",
         anchor_date="2026-02-10", buy_price=None, quantity=None, price_now=None,
         since_anchor_pct=4.1, one_yr_pct=11, valuation_label="Fair - mid range",
         signal="watch", trigger_type="interval",
         reasoning=["No material change since added"]),
]


def seed() -> None:
    init_db()

    with get_market_conn() as m:
        if m.execute("SELECT COUNT(*) c FROM stocks").fetchone()["c"] == 0:
            # Stock identities only — Management/Portfolio join against `stocks` for
            # sector/market, so these rows need to exist. Mock *quarterly financials*
            # are deliberately NOT seeded here anymore: Analytics should only ever show
            # real ingested data (SEC EDGAR today), never a mock placeholder, so a
            # fresh start leaves quarterly_data empty until real ingestion runs via /admin.
            m.executemany(
                "INSERT OR REPLACE INTO stocks (symbol, exchange, name, sector, market) "
                "VALUES (:symbol,:exchange,:name,:sector,:market)",
                STOCKS,
            )
            m.commit()
            MockSource().ingest_prices(m)  # still mock — Management's sparkline, unaffected by this change

    with get_users_conn() as u:
        if u.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] > 0:
            print("users.db already seeded, skipping.")
            return

        u.execute("INSERT OR IGNORE INTO users (email, plan) VALUES (?, 'pro')", (USER_EMAIL,))
        user_id = u.execute("SELECT id FROM users WHERE email = ?", (USER_EMAIL,)).fetchone()["id"]

        for pos in POSITIONS:
            cur = u.execute(
                """INSERT INTO tracked_positions (user_id, symbol, company_name, anchor_type, anchor_date, buy_price, quantity, status)
                VALUES (?,?,?,?,?,?,?, 'active')""",
                (user_id, pos["symbol"], pos["company_name"], pos["anchor_type"], pos["anchor_date"], pos["buy_price"], pos["quantity"]),
            )
            position_id = cur.lastrowid
            u.execute(
                """INSERT INTO position_snapshots
                (position_id, price_at_check, since_anchor_pct, one_yr_pct, valuation_label, signal, reasoning_json, trigger_type)
                VALUES (?,?,?,?,?,?,?,?)""",
                (position_id, pos["price_now"], pos["since_anchor_pct"], pos["one_yr_pct"],
                 pos["valuation_label"], pos["signal"], json.dumps(pos["reasoning"]), pos["trigger_type"]),
            )
        u.commit()

    print(f"Seeded users.db (1 user, {len(POSITIONS)} positions). Market data via MockSource ingestion.")


if __name__ == "__main__":
    seed()
