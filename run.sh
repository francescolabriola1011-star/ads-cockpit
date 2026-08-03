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

# Controllo interno su TUTTI i clienti (anche quelli fuori dalla dashboard
# pubblica) + notifica a Francesco se c'e' qualcosa da decidere oggi.
echo
python3 build.py --tutti --no-anon --out privato \
  --titolo "Controllo regole" --sottotitolo "tutti i clienti" >/dev/null
python3 alert.py

# Stacco automatico sulle campagne di casa (solo AI Elite), prima di
# rigenerare i dati, così la dashboard mostra già lo stato aggiornato.
python3 autopause.py --esegui || echo "autopause: vedi sopra"
echo

if [[ -n "$(git status --porcelain docs/)" ]]; then
  git add docs/
  git commit -q -m "dati $(date '+%Y-%m-%d %H:%M')"
  git push -q origin main
  echo "pubblicato su GitHub Pages"
else
  echo "nessuna variazione, niente push"
fi
