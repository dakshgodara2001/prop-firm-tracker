#!/usr/bin/env bash
# Long-running scheduler for containers (used by docker-compose's `scheduler`
# service; host cron users should keep using scripts/cron_daily.sh instead).
#
# Sleeps until PFT_RUN_AT (HH:MM, default 19:30) in Asia/Kolkata, then runs
# the daily pipeline on weekdays. Exit codes are logged, never fatal — the
# next day's run still happens, and re-runs are idempotent.
set -u
export TZ=Asia/Kolkata
RUN_AT="${PFT_RUN_AT:-19:30}"

echo "scheduler: will run 'python main.py run-daily' at ${RUN_AT} IST on weekdays"
while true; do
    now=$(date +%s)
    target=$(date -d "today ${RUN_AT}" +%s)
    if [ "$target" -le "$now" ]; then
        target=$(date -d "tomorrow ${RUN_AT}" +%s)
    fi
    echo "scheduler: sleeping until $(date -d "@$target" '+%Y-%m-%d %H:%M %Z')"
    sleep $((target - now))
    if [ "$(date +%u)" -le 5 ]; then
        echo "scheduler: running daily pipeline for $(date +%F)"
        python main.py run-daily
        echo "scheduler: run-daily exited $? "
    else
        echo "scheduler: weekend — skipping"
    fi
done
