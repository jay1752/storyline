"""Rules over time (app/backtest.py): the replay must only use what was public on each date. Synthetic data, no database.
Run: python -m unittest tests.test_backtest -v   (from the repo root)"""

import unittest
from datetime import date, timedelta

from app import framework
from app.backtest import Prices, chart_svg, public_on, replay

QCOLS = ["roce_pct", "roe_pct", "debt_equity", "promoter_pledge", "promoter_pledge_pct", "pbt_cr", "finance_cost_cr", "current_assets_cr",
         "current_liabilities_cr", "inventory_cr", "cfo_cr", "total_debt_cr", "net_profit_cr", "operating_profit_cr", "fii_pct"]


def quarter(end, growth=40.0, opm=35.0, promoter=60.0, **kw):
    r = {c: None for c in QCOLS}
    r.update({"symbol": "NSE:TEST", "period_end_date": end, "quarter_label": "Q", "revenue_growth_pct": growth, "opm_pct": opm, "promoter_pct": promoter})
    r.update(kw)
    return r


def quarters(n, last="2026-06-30", **kw):
    ends, d = [], date.fromisoformat(last)
    for _ in range(n):
        ends.append(d.isoformat())
        m = d.month - 3
        d = date(d.year - (1 if m <= 0 else 0), m + 12 if m <= 0 else m, 1)
        d = (date(d.year + (d.month // 12), d.month % 12 + 1, 1) - timedelta(days=1))
    return [quarter(e, **kw) for e in reversed(ends)]


def annual(fy_end, roce, **kw):
    r = {"period_end_date": fy_end, "source": "indianapi", "roce_pct": roce, "roe_pct": roce, "equity_capital_cr": 10.0, "reserves_cr": 90.0, "borrowings_cr": 5.0,
         "revenue_cr": 100.0, "operating_profit_cr": 30.0, "total_assets_cr": 300.0, "cfo_cr": None, "net_profit_cr": None}
    r.update(kw)
    return r


def prices(start="2024-01-01", months=40, base=100.0, step=2.0):
    out, d = [], date.fromisoformat(start)
    for i in range(months):
        out.append((d.isoformat(), base + step * i))
        d = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
    return Prices(out)


TH = framework.builtin()
SZ = framework.sizing_defaults()
TODAY = date(2026, 11, 17)


def roce_status(tl):
    return {p["period_end"]: next(f["status"] for f in p["filters"] if f["key"] == "roce") for p in tl["points"]}


class Availability(unittest.TestCase):
    def test_quarter_is_public_45_days_after_and_march_60(self):
        self.assertEqual(public_on("2026-09-30"), date(2026, 11, 14))
        self.assertEqual(public_on("2026-03-31"), date(2026, 5, 30))

    def test_an_annual_figure_is_used_only_from_90_days_after_its_year_end(self):
        rows = quarters(9)   # 2024-06-30 .. 2026-06-30
        ann = [annual("2025-03-31", 40.0), annual("2026-03-31", 10.0)]
        got = roce_status(replay("NSE:TEST", rows, ann, "Industrials", TH, SZ, prices(), today=TODAY))
        self.assertEqual(got["2025-03-31"], "unknown")   # public 2025-05-30, FY2025 only from 2025-06-29
        self.assertEqual(got["2025-09-30"], "pass")      # FY2025 (ROCE 40)
        self.assertEqual(got["2026-03-31"], "pass")      # FY2026 not yet public on 2026-05-30: still FY2025
        self.assertEqual(got["2026-06-30"], "fail")      # FY2026 (ROCE 10) public from 2026-06-29

    def test_each_point_ignores_later_quarters(self):
        rows = quarters(9)
        rows[-1]["revenue_growth_pct"] = -50.0   # a bad latest quarter
        tl = replay("NSE:TEST", rows, [], "Industrials", TH, SZ, prices(), today=TODAY)
        first = next(f for f in tl["points"][0]["filters"] if f["key"] == "sales_growth")
        last = next(f for f in tl["points"][-1]["filters"] if f["key"] == "sales_growth")
        self.assertEqual(first["status"], "pass")
        self.assertEqual(last["status"], "fail")

    def test_price_to_book_is_never_judged_in_the_past_for_a_financial(self):
        rows = quarters(9, promoter=50.0)
        ann = [annual("2025-03-31", 18.0, total_assets_cr=1000.0), annual("2026-03-31", 18.0, total_assets_cr=1100.0)]
        tl = replay("NSE:TEST", rows, ann, "Financial Services", TH, SZ, prices(), today=TODAY)
        for p in tl["points"]:
            pb = next(f for f in p["filters"] if f["key"] == "pb")
            self.assertEqual(pb["status"], "unknown")
        last = {f["key"]: f["status"] for f in tl["points"][-1]["filters"]}
        self.assertEqual(last["equity_assets"], "pass")   # from the annual balance sheet that was public then


class Shape(unittest.TestCase):
    def test_fewer_than_four_quarters_gives_no_points_and_says_so(self):
        tl = replay("NSE:TEST", quarters(3), [], "Industrials", TH, SZ, prices(), today=TODAY)
        self.assertEqual(tl["points"], [])
        self.assertIn("Fewer than 4", tl["notes"][0])
        self.assertEqual(chart_svg(tl), "")

    def test_points_are_oldest_first_capped_and_changes_are_marked(self):
        rows = quarters(20)
        for r in rows[-3:]:
            r["revenue_growth_pct"] = 5.0    # the last three quarters fail the growth rule
        tl = replay("NSE:TEST", rows, [], "Industrials", TH, SZ, prices(start="2022-01-01", months=60), today=TODAY, max_points=12)
        self.assertEqual(len(tl["points"]), 12)
        self.assertEqual([p["asof"] for p in tl["points"]], sorted(p["asof"] for p in tl["points"]))
        self.assertFalse(tl["points"][0]["changed"])
        self.assertEqual(sum(1 for p in tl["points"] if p["changed"]), sum(1 for a, b in zip(tl["points"], tl["points"][1:]) if a["label"] != b["label"]))

    def test_forward_price_moves_are_plain_and_none_when_the_date_is_not_reached(self):
        rows = quarters(9)
        tl = replay("NSE:TEST", rows, [], "Industrials", TH, SZ, prices(start="2024-01-01", months=34, base=100.0, step=2.0), today=TODAY)
        early, late = tl["points"][0], tl["points"][-1]
        self.assertIsNotNone(early["after"]["12 months"])
        self.assertGreater(early["after"]["3 months"], 0)
        self.assertIsNone(late["after"]["12 months"])    # beyond the last stored price

    def test_chart_is_drawn_with_a_band_per_point_and_text_is_escaped(self):
        tl = replay("NSE:TEST", quarters(9), [], "Industrials", TH, SZ, prices(), today=TODAY)
        tl["points"][0]["label"] = "<b>x</b>"
        svg = chart_svg(tl)
        self.assertTrue(svg.startswith("<svg"))
        self.assertEqual(svg.count("<rect"), len(tl["points"]))
        self.assertNotIn("<b>x</b>", svg)


if __name__ == "__main__":
    unittest.main()
