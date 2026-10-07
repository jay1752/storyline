#!/bin/bash
# Hot-sync front-end files into the running container (templates + static + routes) — no restart needed
# for templates/CSS/JS (Jinja auto-reloads, assets are cache-busted by mtime).
cd "$(dirname "$0")/.."
docker cp app/templates/. storyline-app-1:/app/app/templates/
docker cp app/static/. storyline-app-1:/app/app/static/
