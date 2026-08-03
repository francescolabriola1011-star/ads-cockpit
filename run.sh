#!/bin/bash
# ADS COCKPIT — pull Meta, ricalcolo regole, push su GitHub Pages.
# Lanciato ogni mattina alle 08:00 da launchd, o a mano.
set -euo pipefail
cd "$(dirname "$0")"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') ==="

python3 build.py "$@"

# push solo se i dati sono cambiati davvero
if [[ -n "$(git status --porcelain docs/)" ]]; then
  git add docs/
  git commit -q -m "dati $(date '+%Y-%m-%d %H:%M')"
  git push -q origin main
  echo "pubblicato su GitHub Pages"
else
  echo "nessuna variazione, niente push"
fi
