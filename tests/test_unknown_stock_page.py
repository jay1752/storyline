"""The unknown-stock page escapes what was typed and is a real page.
Run: python -m unittest tests.test_unknown_stock_page -v   (from the repo root)"""

import sqlite3
import unittest
from collections import defaultdict
from contextlib import contextmanager
from unittest import mock

from starlette.testclient import TestClient

class UnknownStockPage(unittest.TestCase):
    def setUp(self):
        self.market = sqlite3.connect(":memory:", check_same_thread=False)
        self.market.row_factory = sqlite3.Row
        self.market.execute("CREATE TABLE stocks (symbol TEXT, name TEXT)")
        self.market.execute("INSERT INTO stocks VALUES ('NSE:LISTED', 'Listed Co Ltd')")
        user = defaultdict(lambda: None, id=1, email="t@example.com", is_platform_admin=0)

        @contextmanager
        def conn():
            yield self.market

        from app.main import app
        self.client = TestClient(app)
        for p in (mock.patch("app.auth.middleware.get_session_user", return_value=user),
                  mock.patch("app.routes.analytics._cached_stock_context", return_value=None),
                  mock.patch("app.routes.analytics.get_market_conn", conn)):
            p.start()
            self.addCleanup(p.stop)
        self.client.cookies.set("session_id", "x")

    def test_typed_text_is_escaped_not_run(self):
        r = self.client.get("/analytics/%3Cscript%3Ealert(1)%3C/script%3E")
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("<script>alert(1)</script>", r.text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", r.text)

    def test_it_is_a_real_page_with_a_way_forward(self):
        r = self.client.get("/analytics/NSE:NOPE")
        self.assertEqual(r.status_code, 404)
        self.assertIn("couldn't find that stock", r.text)
        self.assertIn('hx-get="/search-stocks"', r.text)
        self.assertIn("Back to Analytics", r.text)
        self.assertIn("<nav", r.text)   # inside the normal app shell, not a bare fragment

    def test_a_listed_company_without_results_says_so(self):
        r = self.client.get("/analytics/NSE:LISTED")
        self.assertEqual(r.status_code, 404)
        self.assertIn("No reported results for Listed Co Ltd yet", r.text)

    def test_a_very_long_symbol_is_cut(self):
        r = self.client.get("/analytics/" + "A" * 500)
        self.assertNotIn("A" * 100, r.text)


if __name__ == "__main__":
    unittest.main()
