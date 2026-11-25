#!/bin/sh
# Called by cron. Logs to ~/etl-digest/digest.log
cd "$(dirname "$0")" || exit 1
echo "=== $(date '+%F %T')" >> digest.log
/usr/bin/python3 -I ./etl_digest.py >> digest.log 2>&1
