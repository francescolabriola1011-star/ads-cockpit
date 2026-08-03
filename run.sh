#!/bin/bash
# ADS COCKPIT — pull Meta, ricalcolo regole, push su GitHub Pages.
# Genera DUE dashboard dallo stesso motore:
#   docs/      -> generale, con selettore account (quella condivisibile)
#   docs/aea/  -> solo AI Elite Advisory, casa nostra
set -euo pipefail
cd "$(dirname "$0")"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') ==="

python3 build.py --titolo "ADS Cockpit" --sottotitolo "tutti gli account"
echo
python3 build.py --account act_859478370532394 \
  --out docs/aea --titolo "AI Elite Advisory" --sottotitolo "casa nostra"

if [[ -n "$(git status --porcelain docs/)" ]]; then
  git add docs/
  git commit -q -m "dati $(date '+%Y-%m-%d %H:%M')"
  git push -q origin main
  echo "pubblicato su GitHub Pages"
else
  echo "nessuna variazione, niente push"
fi
