#!/usr/bin/env bash
set -euo pipefail
git config user.name 'github-actions[bot]'
git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
for directory in receipts results; do
  if [ -d "$directory" ]; then
    git add -A -- "$directory"
  fi
done
if git diff --cached --quiet; then
  echo 'No receipt changes.'
  exit 0
fi
git commit -m 'Record fixed-window G5 aggregate receipt'
git push
