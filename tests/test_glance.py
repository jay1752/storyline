"""Growth and returns at a glance (app/glance.py). Pure maths plus in-memory databases. Run: python -m unittest tests.test_glance -v"""

import sqlite3
import unittest
from datetime import date, timedelta

from app import glance as G


def monthly_prices(end, years, annual_growth, start_price=100.0):
    out, n = [], years * 12
    e = date.fromisoformat(end)
    for i in range(n, -1, -1):
        d = (e.replace(day=15) - timedelta(days=int(30.4375 * i)))
        out.append((d.isoformat(), start_price * (1 + annual_growth) ** ((n - i) / 12)))
    out[-1] = (e.isoformat(), out[-1][1])
    return out


def fy(year, sales, profit, roe=None):
    return {"fy_end": f"{year}-03-31", "sales": sales, "profit": profit, "roe": roe}


class Maths(unittest.TestCase):
    def test_cagr(self):
        self.assertAlmostEqual(G.cagr(200, 100, 5), 14.87, places=2)
        self.assertAlmostEqual(G.cagr(50, 100, 5), -12.94, places=2)
        for bad in ((100, 0), (100, -5), (-5, 100), (None, 100), (100, None)):
            self.assertIsNone(G.cagr(bad[0], bad[1], 5))

    def test_price_cagr_for_a_steady_ten_percent_grower(self):
        r = G.price_cagrs(monthly_prices("2026-10-31", 11, 0.10))
        for h in ("10Y", "5Y", "3Y", "1Y"):
            self.assertAlmostEqual(r[h], 10.0, delta=0.6)

    def test_a_stock_listed_two_years_ago_has_only_the_horizons_it_has_lived(self):
        r = G.price_cagrs(monthly_prices("2026-10-31", 2, 0.20))
        self.assertIsNone(r["10Y"])
        self.assertIsNone(r["5Y"])
        self.assertIsNone(r["3Y"])
        self.assertAlmostEqual(r["1Y"], 20.0, delta=1.0)

    def test_a_price_series_with_a_big_hole_gives_no_rate_for_that_horizon(self):
        full = monthly_prices("2026-10-31", 11, 0.10)
        gappy = [p for p in full if not ("2021-08-01" <= p[0] <= "2021-12-31")]   # nothing near five years back
        self.assertIsNone(G.price_cagrs(gappy)["5Y"])
        self.assertIsNotNone(G.price_cagrs(gappy)["10Y"])

    def test_annual_cagr_uses_the_latest_year_against_the_one_n_years_earlier(self):
        s = [fy(y, 100.0 * 1.2 ** (y - 2016), 10.0 * 1.3 ** (y - 2016)) for y in range(2016, 2027)]
        self.assertAlmostEqual(G.annual_cagr(s, "sales", 10), 20.0, places=1)
        self.assertAlmostEqual(G.annual_cagr(s, "profit", 5), 30.0, places=1)

    def test_a_loss_or_missing_base_year_gives_no_compounded_rate(self):
        s = [fy(2021, 100.0, -5.0), fy(2022, 110.0, 4.0), fy(2023, 120.0, 6.0), fy(2024, 130.0, 8.0), fy(2025, 140.0, 10.0), fy(2026, 150.0, 12.0)]
        self.assertIsNone(G.annual_cagr(s, "profit", 5))        # the base year lost money
        self.assertIsNotNone(G.annual_cagr(s, "sales", 5))
        self.assertIsNone(G.annual_cagr(s, "sales", 10))        # not enough years
        self.assertIsNone(G.annual_cagr([], "sales", 3))

    def test_roe_is_the_average_of_exactly_n_years(self):
        s = [fy(y, 1, 1, roe) for y, roe in zip(range(2022, 2027), (10.0, 20.0, 30.0, 40.0, 50.0))]
        self.assertEqual(G.roe_average(s, 3), 40.0)
        self.assertEqual(G.roe_average(s, 5), 30.0)
        self.assertIsNone(G.roe_average(s, 10))
        self.assertIsNone(G.roe_average([fy(2025, 1, 1, None), fy(2026, 1, 1, 20.0)], 2))

    def test_ttm_growth_is_four_quarters_against_the_four_before(self):
        ends = ["2024-12-31", "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30"]
        q = [{"period_end_date": e, "revenue_cr": v} for e, v in zip(ends, (100, 100, 100, 100, 120, 120, 120, 120))]
        self.assertAlmostEqual(G.ttm_growth(q, "revenue_cr"), 20.0)

    def test_ttm_needs_consecutive_quarters_and_positive_profit(self):
        ends = ["2024-12-31", "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30"]
        q = [{"period_end_date": e, "net_profit_cr": 10} for e in ends]
        del q[3]
        self.assertIsNone(G.ttm_growth(q, "net_profit_cr"))
        loss = [{"period_end_date": e, "net_profit_cr": -1 if i < 4 else 5} for i, e in enumerate(ends)]
        self.assertIsNone(G.ttm_growth(loss, "net_profit_cr"))


class Assembly(unittest.TestCase):
    def test_the_vendors_figure_wins_and_our_own_fills_the_gaps(self):
        own = {"sales": {"10Y": 11.0, "5Y": 12.0, "3Y": 13.0, "TTM": 14.0}, "profit": {}, "price": {"1Y": -30.4}, "roe": {}}
        ia = {"Compounded Sales Growth": {"10Y": 20.2, "5Y": None}}
        cards = G.build(own, ia)
        sales = {r["label"]: (r["value"], r["from_vendor"]) for r in cards[0]["rows"]}
        self.assertEqual(sales["10 Years"], (20, True))             # IndianAPI's, rounded
        self.assertEqual(sales["5 Years"], (12, False))             # a blank from the vendor is filled by ours
        self.assertEqual(sales["TTM"], (14, False))
        self.assertTrue(cards[1]["empty"])
        self.assertEqual({r["label"]: r["value"] for r in cards[2]["rows"]}["1 Year"], -30)

    def test_price_always_comes_from_our_own_current_prices_the_vendors_is_only_a_last_resort(self):
        own = {"price": {"1Y": -30.4, "3Y": None}}
        ia = {"Stock Price CAGR": {"1Y": -22.0, "3Y": -5.0}}
        price = {r["label"]: r["value"] for r in G.build(own, ia)[2]["rows"]}
        self.assertEqual(price["1 Year"], -30)        # ours, not the older -22
        self.assertEqual(price["3 Years"], -5)        # we cannot compute it (young listing): the vendor's stands in

    def test_a_compounded_rate_above_100_percent_over_five_years_or_more_is_shown_as_a_dash(self):
        ia = {"Compounded Sales Growth": {"10Y": 110.0, "5Y": 130.0, "3Y": 140.0, "TTM": 150.0}}
        rows = {r["label"]: r for r in G.build({}, ia)[0]["rows"]}
        self.assertEqual((rows["10 Years"]["value"], rows["10 Years"]["suspect"]), (None, True))
        self.assertEqual(rows["5 Years"]["value"], None)
        self.assertEqual((rows["3 Years"]["value"], rows["TTM"]["value"]), (140, 150))   # short horizons can be genuinely high
        self.assertEqual(G.build({}, {"Stock Price CAGR": {"10Y": 120.0}})[2]["rows"][0]["value"], 120)   # a price rate is not a base-year artefact

    def test_the_four_cards_have_the_four_rows_each_in_the_published_order(self):
        cards = G.build({}, None)
        self.assertEqual([c["title"] for c in cards], ["Compounded Sales Growth", "Compounded Profit Growth", "Stock Price CAGR", "Return on Equity"])
        self.assertEqual([r["label"] for r in cards[0]["rows"]], ["10 Years", "5 Years", "3 Years", "TTM"])
        self.assertEqual([r["label"] for r in cards[2]["rows"]], ["10 Years", "5 Years", "3 Years", "1 Year"])
        self.assertEqual([r["label"] for r in cards[3]["rows"]], ["10 Years", "5 Years", "3 Years", "Last Year"])


def make_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE price_history (symbol TEXT, date TEXT, close REAL)")
    c.execute("CREATE TABLE annual_data (symbol TEXT, period_end_date TEXT, revenue_cr REAL, net_profit_cr REAL, roe_pct REAL)")
    c.execute("CREATE TABLE us_annual (symbol TEXT, period_end_date TEXT, revenue_m REAL, net_profit_m REAL, equity_m REAL)")
    c.execute("CREATE TABLE quarterly_data (symbol TEXT, period_end_date TEXT, quarter_label TEXT, revenue_cr REAL, net_profit_cr REAL)")
    return c


class FromTheDatabase(unittest.TestCase):
    def test_an_indian_stock_is_filled_from_annual_figures_and_prices_where_the_vendor_has_nothing(self):
        c = make_db()
        for d, p in monthly_prices(date.today().isoformat(), 11, 0.10):
            c.execute("INSERT INTO price_history VALUES ('NSE:AAA', ?, ?)", (d, p))
        for y in range(2016, 2027):
            c.execute("INSERT INTO annual_data VALUES ('NSE:AAA', ?, ?, ?, ?)", (f"{y}-03-31", 100.0 * 1.2 ** (y - 2016), 10.0 * 1.3 ** (y - 2016), 25.0))
        out = G.for_stock(c, "NSE:AAA", "IN", None)
        rows = {card["title"]: {r["label"]: r["value"] for r in card["rows"]} for card in out["cards"]}
        self.assertEqual(rows["Compounded Sales Growth"]["10 Years"], 20)
        self.assertEqual(rows["Compounded Profit Growth"]["5 Years"], 30)
        self.assertEqual(rows["Return on Equity"]["3 Years"], 25)
        self.assertAlmostEqual(rows["Stock Price CAGR"]["5 Years"], 10, delta=1)
        self.assertFalse(out["vendor"])

    def test_a_us_stock_uses_our_own_annual_and_price_data_and_ignores_the_indian_vendor_block(self):
        c = make_db()
        for d, p in monthly_prices(date.today().isoformat(), 6, 0.15):
            c.execute("INSERT INTO price_history VALUES ('NASDAQ:BBB', ?, ?)", (d, p))
        for y in range(2020, 2027):
            c.execute("INSERT INTO us_annual VALUES ('NASDAQ:BBB', ?, ?, ?, ?)", (f"{y}-12-31", 1000.0 * 1.1 ** (y - 2020), 100.0 * 1.1 ** (y - 2020), 500.0))
        out = G.for_stock(c, "NASDAQ:BBB", "US", {"Compounded Sales Growth": {"10Y": 99.0}})
        rows = {card["title"]: {r["label"]: r["value"] for r in card["rows"]} for card in out["cards"]}
        self.assertEqual(rows["Compounded Sales Growth"]["5 Years"], 10)
        self.assertIsNone(rows["Compounded Sales Growth"]["10 Years"])            # the Indian vendor's 99 is not used for a US stock
        self.assertEqual(rows["Return on Equity"]["3 Years"], round(sum(100.0 * 1.1 ** k / 500.0 * 100 for k in (4, 5, 6)) / 3))

    def test_nothing_known_gives_no_cards(self):
        self.assertIsNone(G.for_stock(make_db(), "NSE:NONE", "IN", None))


class Rendering(unittest.TestCase):
    def test_the_partial_shows_values_dashes_and_the_plain_note(self):
        from app.templates_config import templates
        cards = G.build({"sales": {"10Y": 20.0, "5Y": 44.0}, "price": {"3Y": -5.0}}, None)
        html = templates.env.get_template("_glance.html").render(glance={"cards": cards, "vendor": False})
        self.assertIn("Compounded Sales Growth", html)
        self.assertIn(">20%<", html)
        self.assertIn(">-5%<", html)
        self.assertIn("Not enough history yet", html)         # the profit card has nothing
        self.assertIn("leaves out dividends", html)
        self.assertNotIn("pos", html.replace("position", ""))  # no good/bad colouring


if __name__ == "__main__":
    unittest.main()
