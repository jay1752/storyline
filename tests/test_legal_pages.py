"""The Privacy and Terms pages must keep saying what the product does with emails, trades and returns. Run: python -m unittest tests.test_legal_pages -v"""

import unittest

from starlette.testclient import TestClient


class LegalPages(unittest.TestCase):
    def setUp(self):
        from app.main import app
        self.c = TestClient(app, follow_redirects=False)

    def test_privacy_covers_the_emails_the_data_and_the_provider(self):
        r = self.c.get("/privacy")
        self.assertEqual(r.status_code, 200)
        for text in ("Emails we send you", "Weekly rule email (on by default)", "Once a week, on Monday morning India time", "We do not send marketing or promotional emails",
                     "Resend", "Your trades and returns", "Status history and alerts", "turn these emails off in one step", "Delete my account", "18 November 2026"):
            self.assertIn(text, r.text)

    def test_privacy_no_longer_says_deletion_needs_an_email(self):
        self.assertNotIn("Ask us to export or delete your data by emailing", self.c.get("/privacy").text)

    def test_terms_cover_alerts_returns_history_and_tax(self):
        r = self.c.get("/terms")
        self.assertEqual(r.status_code, 200)
        for text in ("Alerts, returns and history", "not a monitoring service", "the weekly rule email, which is on by default", "leave out brokerage, taxes and dividends".replace("leave out", "They leave out"),
                     "not tax advice and not a tax calculation", "do not predict the future", "does not give investment advice"):
            self.assertIn(text, r.text)

    def test_both_are_public_without_signing_in(self):
        for path in ("/privacy", "/terms"):
            self.assertEqual(self.c.get(path).status_code, 200)


if __name__ == "__main__":
    unittest.main()
