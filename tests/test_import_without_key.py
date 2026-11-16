"""A spreadsheet statement imports with no AI key; anything that needs the AI still asks for one.
Run: python -m unittest tests.test_import_without_key -v   (from the repo root)"""

import asyncio
import sqlite3
import unittest
from unittest import mock

CSV = b"Symbol,ISIN,Quantity,Average Price\nTCS,INE467B01029,10,3500\nINFY,INE009A01021,5,1500\n"
PROPOSAL = {"rows": [{"symbol": "NSE:TCS"}, {"symbol": "NSE:INFY"}], "skipped": []}


def run_stream(uploads, message="", chain=()):
    from app.routes import chat
    users = sqlite3.connect(":memory:")
    users.row_factory = sqlite3.Row
    users.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, import_consent_at TEXT)")
    users.execute("INSERT INTO users (id) VALUES (1)")
    frames = []

    async def go():
        async for f in chat._stream(1, message, None, None, None, uploads):
            frames.append(f)

    with mock.patch.object(chat, "build_chain", return_value=list(chain)), \
         mock.patch.object(chat, "get_user_settings", return_value={"provider": "gemini"}), \
         mock.patch.object(chat, "page_instructions", return_value=""), \
         mock.patch.object(chat.activity, "log"), \
         mock.patch("app.db.get_users_conn", return_value=users), \
         mock.patch.object(chat.positions_mod, "propose", return_value=PROPOSAL):
        asyncio.run(go())
    return "".join(frames)


class ImportWithoutKey(unittest.TestCase):
    def test_a_spreadsheet_imports_with_no_ai_key(self):
        out = run_stream([("holdings.csv", CSV)])
        self.assertIn('"type": "proposal"', out)
        self.assertIn("Read your holdings file directly", out)
        self.assertNotIn("No API key", out)

    def test_several_spreadsheets_import_with_no_ai_key(self):
        out = run_stream([("a.csv", CSV), ("b.csv", CSV)])
        self.assertIn('"type": "proposal"', out)
        self.assertNotIn("No API key", out)

    def test_plain_chat_without_a_key_still_asks_for_one(self):
        out = run_stream([], message="hello")
        self.assertIn("No API key for", out)

    def test_a_file_the_reader_cannot_map_needs_the_key_because_the_ai_would_read_it(self):
        out = run_stream([("weird.csv", b"foo,bar\n1,2\n")])
        self.assertIn("No API key for", out)
        self.assertNotIn('"type": "proposal"', out)

    def test_a_pdf_without_a_key_asks_for_one(self):
        out = run_stream([("statement.pdf", b"%PDF-1.4 not really")])
        self.assertIn("No API key for", out)


if __name__ == "__main__":
    unittest.main()
