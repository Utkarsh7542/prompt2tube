#!/usr/bin/env bash
# One container runs BOTH the web app and the render worker.
#
# WHY BOTH IN ONE CONTAINER
# -------------------------
# The web app and the worker share one SQLite job database. A separate Render
# service would need a shared persistent disk, which the free tier does not
# provide, so they must live together. The worker runs in the background; the
# web server runs in the foreground and keeps the container alive.
#
# If the worker ever exits, it is restarted. On restart its recover() re-attaches
# to any Runpod render that was already submitted (and paid for), so an
# interrupted render is collected rather than lost.
#
# NOTE (free tier): the disk is ephemeral, so the job DB and any in-progress
# renders are wiped on a redeploy. Fine for a demo; a paid disk or external DB
# is needed before this is a durable service.
set -uo pipefail

( while true; do
    echo "[start] launching render worker"
    python worker.py || echo "[start] worker exited ($?); restarting in 3s"
    sleep 3
  done ) &

exec gunicorn --workers 1 --timeout 120 --bind "0.0.0.0:${PORT:-5000}" app:app
