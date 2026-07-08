#!/usr/bin/env bash
# Cron wrapper: resolves the project directory, prefers the project venv,
# and forwards any extra arguments to `main.py run-daily`.
#
# Example crontab (19:30 IST, Mon-Fri; NSE publishes deals ~18:30 IST):
#   CRON_TZ=Asia/Kolkata
#   30 19 * * 1-5 /path/to/prop-firm-tracker/scripts/cron_daily.sh >> /path/to/prop-firm-tracker/logs/cron.log 2>&1
#
# If your cron daemon lacks CRON_TZ, schedule in the machine's local time
# equivalent of ~19:30 IST. Exit code 2 means a bulk/block fetch failed.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
mkdir -p logs

PYTHON="$PROJECT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    PYTHON="$(command -v python3)"
fi

exec "$PYTHON" main.py run-daily "$@"
