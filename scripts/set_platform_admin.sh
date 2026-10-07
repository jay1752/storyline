#!/bin/bash
# Grant or revoke platform-admin (access to the Admin section: ingestion pipeline and scheduled jobs).
#   ./scripts/set_platform_admin.sh someone@example.com        # grant
#   ./scripts/set_platform_admin.sh someone@example.com 0      # revoke
# Takes effect on that user's next page load. Lists current admins afterwards.
set -e
EMAIL="$1"; FLAG="${2:-1}"
[ -z "$EMAIL" ] && { echo "usage: $0 <email> [1|0]"; exit 1; }
docker exec -i storyline-app-1 python3 - "$EMAIL" "$FLAG" << 'PY'
import sqlite3, sys
email, flag = sys.argv[1].strip().lower(), int(sys.argv[2])
c = sqlite3.connect('/app/data/users.db')
n = c.execute("UPDATE users SET is_platform_admin = ? WHERE lower(email) = ?", (flag, email)).rowcount
c.commit()
print(("updated " + email) if n else ("no user with email " + email))
print("platform admins now:", [r[0] for r in c.execute("SELECT email FROM users WHERE is_platform_admin = 1")])
PY
