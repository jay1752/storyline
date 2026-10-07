#!/bin/bash
# Daily cron trigger for the Insights job (app/ai/insights_engine.py) — one new
# position_snapshots row per tracked position per day, building the running story.
# Same shape as trigger_tiingo_backfill.sh. Scheduled a bit after the price backfills
# so same-day numbers are fresh, though insights_engine currently reads quarterly_data
# rather than price_history directly — kept consistent anyway for when that changes.

LOG_FILE="/Users/jaydeepsarraf/IdeaProjects/storyline/scripts/insights.log"

# /admin/* now requires a logged-in session — see trigger_tiingo_backfill.sh's identical
# comment. SESSION_ID is also what decides WHICH user's Insights run this triggers.
SESSION_ID="$(cat /Users/jaydeepsarraf/IdeaProjects/storyline/scripts/.admin_session)"

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) triggering daily insights run" >> "$LOG_FILE"
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/admin/run-insights \
  -H "Cookie: session_id=${SESSION_ID}" \
  >> "$LOG_FILE" 2>&1
