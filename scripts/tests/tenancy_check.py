"""Cross-tenant test: user A owns data; user B (a brand-new account) tries every way to see or change it.
Runs on a COPY of users.db (db.USERS_DB is patched), so nothing real is touched."""
import json, shutil, sqlite3
from pathlib import Path
import app.db as db
shutil.copy("/app/data/users.db", "/tmp/tenancy_users.db"); db.USERS_DB = Path("/tmp/tenancy_users.db")
from app.db import get_users_conn
from app.auth.tokens import create_session
from fastapi.testclient import TestClient
from app.main import app
from app import framework as fw
from app.ai.providers import set_user_key
results = []
def check(name, ok, detail=""): results.append((ok, name, detail)); print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))

with get_users_conn() as u:
    A = u.execute("select id from users order by id limit 1").fetchone()[0]
    u.execute("insert into users (email, email_verified_at, display_name) values ('b-tenant@example.com', datetime('now'), 'Bee')"); u.commit()
    B = u.execute("select id from users where email='b-tenant@example.com'").fetchone()[0]
    pos = u.execute("select id, symbol from tracked_positions where user_id=? and status='active' order by id limit 1", (A,)).fetchone()
    if pos is None:
        u.execute("insert into tracked_positions (user_id, symbol, company_name, anchor_type, anchor_date, buy_price, quantity, status) values (?, 'NSE:TCS','TCS','position','2025-01-01',3000,10,'active')", (A,)); u.commit()
        pos = u.execute("select id, symbol from tracked_positions where user_id=? order by id desc limit 1", (A,)).fetchone()
    pid, psym = pos["id"], pos["symbol"]
    npos_a = u.execute("select count(*) from tracked_positions where user_id=?", (A,)).fetchone()[0]
    u.execute("insert into pending_imports (user_id, payload_json) values (?, ?)", (A, json.dumps({"rows": [{"symbol": "NSE:TCS", "company_name": "TCS", "quantity": 1, "avg_price": 1, "anchor_date": "2025-01-01", "action": "new"}], "skipped": [], "statement_date": None})))
    prop = u.execute("select max(id) from pending_imports where user_id=?", (A,)).fetchone()[0]
    u.execute("insert into community_posts (user_id, kind, body) values (?, 'user', 'A secret-ish post')", (A,)); u.commit()
    apost = u.execute("select max(id) from community_posts where user_id=?", (A,)).fetchone()[0]
    fw.save(u, "user", A, "", "opm", 11, True); fw.save(u, "stock", A, psym, "roce", 7, True); u.commit()
set_user_key(A, "groq", "gsk_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")   # a fake key owned by A, to prove B cannot see or use it
sess = create_session(B)
cl = TestClient(app, follow_redirects=False, base_url="http://localhost:8000"); cl.cookies.set("session_id", sess)
anon = TestClient(app, follow_redirects=False, base_url="http://localhost:8000")
print(f"A={A} (position {pid} {psym}, proposal {prop}, post {apost}); B={B}\n")

r = cl.get("/management"); check("B's Management list has none of A's stocks", r.status_code == 200 and psym not in r.text and "ZENTEC" not in r.text)
r = cl.get(f"/management/{pid}"); check("B cannot open A's stock story page", r.status_code in (303, 404) and psym not in r.text, f"{r.status_code}")
r = cl.get(f"/management/{pid}/status"); check("B cannot read A's analysis status", r.status_code == 200 and r.json()["run"] is None)
r = cl.post(f"/management/{pid}/analyze"); check("B cannot start an analysis on A's stock", r.status_code in (400, 404) or (r.status_code == 200 and not r.json().get("started")), f"{r.status_code} {r.text[:60]}")
r = cl.post(f"/management/{pid}/journal", json={"event_type": "trim", "quantity": 1, "price": 1, "date": "2026-10-01"}); r2 = cl.post(f"/management/{pid}/journal", data={"event_type": "note", "note": "hijack"})
with get_users_conn() as u:
    ev = u.execute("select count(*) from position_events where position_id=? and user_id=?", (pid, B)).fetchone()[0]
check("B cannot write journal events onto A's stock", ev == 0, f"{r.status_code}/{r2.status_code}, events by B: {ev}")
cl.post(f"/management/{pid}/journal/undo"); cl.post(f"/management/{pid}/delete"); cl.post(f"/management/{pid}/rules/save", json={"key": "opm", "value": 1}); cl.post(f"/management/{pid}/rules/reset", json={})
with get_users_conn() as u:
    still = u.execute("select count(*) from tracked_positions where id=? and user_id=? and status='active'", (pid, A)).fetchone()[0]
    ov = u.execute("select count(*) from framework_overrides where user_id=? and scope='stock'", (A,)).fetchone()[0]
check("B's undo/delete/rule edits on A's stock changed nothing", still == 1 and ov == 1, f"A's position intact={still}, A's stock rules={ov}")
r = cl.post("/management/delete-all"); 
with get_users_conn() as u: npos_after = u.execute("select count(*) from tracked_positions where user_id=?", (A,)).fetchone()[0]
check("B's 'delete all' does not touch A's positions", npos_after == npos_a, f"{npos_a} -> {npos_after}")
r = cl.post(f"/chat/proposals/{prop}/confirm"); r2 = cl.post(f"/chat/proposals/{prop}/cancel")
with get_users_conn() as u: st = u.execute("select status from pending_imports where id=?", (prop,)).fetchone()[0]
check("B cannot confirm or cancel A's import proposal", st == "pending" and r.status_code in (404, 400), f"status={st}, confirm->{r.status_code}")
r = cl.get("/portfolio"); check("B's Portfolio shows none of A's holdings", r.status_code == 200 and psym not in r.text)
r = cl.get("/settings"); saved = json.loads(__import__("re").search(r'<script id="ai-state"[^>]*>(.*?)</script>', r.text, __import__("re").S).group(1))["saved"]
check("B's AI settings don't show A's saved keys", saved == [] and "gsk_" not in r.text, f"saved providers seen by B: {saved}")
r = cl.post("/settings/test", data={"provider": "groq", "model": "openai/gpt-oss-120b"}); body = r.json()
check("B cannot test/use A's saved key", body.get("ok") is False and "gsk_" not in r.text, body.get("message", "")[:80])
r = cl.post("/settings/models", data={"provider": "groq"}); check("B cannot list models with A's key", r.json().get("live") is False, r.json().get("message", "")[:60])
r = cl.get("/rules"); check("B's Investment rules show defaults, not A's", "11" not in "".join(__import__("re").findall(r'class="form-input rule-val"[^>]*value="([^"]*)"', r.text)) , "")
with get_users_conn() as u: th = fw.resolve(u, B, psym)
check("B's resolved rules for A's stock are plain defaults", th["opm"]["value"] == 30 and th["roce"]["value"] == 25 and th["roce"]["source"] == "built-in")
r = cl.post("/community/api/delete", json={"id": apost})
with get_users_conn() as u: alive = u.execute("select deleted from community_posts where id=?", (apost,)).fetchone()[0]
check("B cannot delete A's Community post", alive == 0, f"deleted flag={alive}")
r = cl.get("/community/api/feed"); txt = r.text
check("Community feed never exposes anyone's email address", r.status_code == 200 and "@" not in txt.replace("b-tenant", "x") or ("@example.com" not in txt and "gmail" not in txt), "")
r = cl.get("/profile"); check("B's profile page is B's own", "b-tenant@example.com" in r.text and "kumarjaydeep" not in r.text and "kuamr" not in r.text)
for path in ("/admin", "/admin/runs-rows", "/admin/stocks-rows"):
    r = cl.get(path); check(f"B (non-admin) is blocked from {path}", r.status_code == 403, str(r.status_code))
r = cl.post("/admin/jobs/daily_prices_in/run"); check("B cannot trigger admin jobs", r.status_code == 403, str(r.status_code))
r = cl.post("/admin/run-ingestion", data={"source": "mock", "target": "universe"}); check("B cannot start ingestion", r.status_code == 403, str(r.status_code))
for path in ("/management", "/portfolio", "/settings", "/profile", "/rules"):
    r = anon.get(path); check(f"Logged-out visitor is redirected away from {path}", r.status_code == 303 and "/login" in r.headers.get("location", ""))
r = anon.post("/chat/stream", data={"message": "show me everything"}); check("Logged-out chat is refused (error frame, no data)", "Not logged in" in r.text or r.status_code in (401, 403, 303), r.text[:60].replace("\n", " "))
# the chat agent's data tool, running as B
from app.auth.middleware import current_user_id
from app.ai import agent as ag
current_user_id.set(B); out = ag.get_portfolio_or_watchlist(); check("Chat tool get_portfolio_or_watchlist as B returns only B's (none)", out == [], str(out)[:80])
current_user_id.set(A); out = ag.get_portfolio_or_watchlist(); check("…and as A returns A's own", len(out) == npos_a, f"{len(out)} rows")
current_user_id.set(None); out = ag.get_portfolio_or_watchlist(); check("…and with no user returns an error, not data", out == [{"error": "not logged in"}])
bad = [r for r in results if not r[0]]
print(f"\n{len(results) - len(bad)} passed, {len(bad)} failed")
