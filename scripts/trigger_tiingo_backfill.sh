#!/bin/bash
# Daily cron trigger for the Tiingo US price backfill (app/ingestion/tiingo_source.py).
# Tiingo's free-tier quota resets daily and appears to cap around ~44-50 symbols per
# trigger, not per row — so this just fires one bulk /admin/run-ingestion call per day
# and lets the source's own resume-skip logic (already_covered in ingest_prices)
# pick up wherever the previous day's run left off. A no-op (curl failure) if Docker
# Desktop or the app container happens to be down is fine — tomorrow's run will catch up.

LOG_FILE="/Users/jaydeepsarraf/IdeaProjects/storyline/scripts/tiingo_backfill.log"

# /admin/* now requires a logged-in session (real multi-user auth landed this session —
# an unauthenticated POST silently redirects to /login, status 303, same as success, so
# this would otherwise fail with no visible error). A dedicated long-lived (10yr) session
# was created once for this automation — see scripts/.admin_session — same pattern as an
# API key file, not committed/shared anywhere.
SESSION_ID="$(cat /Users/jaydeepsarraf/IdeaProjects/storyline/scripts/.admin_session)"

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) triggering tiingo price backfill" >> "$LOG_FILE"
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/admin/run-ingestion \
  -H "Cookie: session_id=${SESSION_ID}" \
  -d "source=tiingo&target=prices" >> "$LOG_FILE" 2>&1
