#!/bin/bash
set -euo pipefail
cd /home/jason/main-line-trades-replays
for n in $(seq 1 30); do
  echo "--- poll $n $(date -u +%H:%M:%S) ---"
  .venv/bin/python -m scripts.poll_review_drafts || true
  if pgrep tesseract >/dev/null; then echo tesseract_running; else echo tesseract_idle; fi
  journalctl -u mainline-earnings-review.service --since "3 min ago" --no-pager -l 2>/dev/null \
    | grep -i "Could not create Signal Review" | tail -1 || true
  if .venv/bin/python -m scripts.poll_review_drafts | grep -q 'ready_count=4'; then
    echo ALL_READY
    exit 0
  fi
  sleep 90
done
exit 1
