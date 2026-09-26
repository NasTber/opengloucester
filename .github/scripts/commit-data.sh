#!/usr/bin/env bash
# Commit changes under the given paths and push to main.
# Usage: commit-data.sh "message" path [path...]
set -euo pipefail
message="$1"; shift
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -- "$@"
if git diff --cached --quiet; then
  echo "No changes in $*."
  exit 0
fi
git commit -m "$message $(TZ=America/New_York date +%Y-%m-%d)"
# Code may have been pushed while this job ran; replay the data commit on top.
for attempt in 1 2 3; do
  if git pull --rebase origin main && git push; then
    exit 0
  fi
  sleep $((attempt * 10))
done
echo "Could not push data commit." >&2
exit 1
