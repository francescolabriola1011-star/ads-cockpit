#!/bin/bash
# Installa un nuovo token Meta preso dal Graph API Explorer.
# Controlla i permessi, lo allunga a 60 giorni, lo salva e verifica che
# lo stacco automatico ora funzioni.
#
# Uso:  ./installa-token.sh "EAAM...."
set -euo pipefail
cd "$(dirname "$0")"

TOK="${1:-}"
[[ -z "$TOK" ]] && { echo "Uso: ./installa-token.sh \"TOKEN\""; exit 1; }

TOK="$TOK" python3 - <<'PY'
import json, os, urllib.request, urllib.parse

tok = os.environ["TOK"].strip()
CFG = os.path.expanduser("~/.config/meta-ads/config.json")


def get(path, **p):
    u = f"https://graph.facebook.com/v21.0/{path}?" + urllib.parse.urlencode(p)
    return json.load(urllib.request.urlopen(u))


# 1. il token e' vivo e ha i permessi giusti?
try:
    d = get("debug_token", input_token=tok, access_token=tok)["data"]
except urllib.error.HTTPError as e:
    print("Token non valido:", json.loads(e.read())["error"]["message"])
    raise SystemExit(1)

scopes = set(d.get("scopes", []))
print("permessi:", ", ".join(sorted(scopes)))
if "ads_management" not in scopes:
    print("\nMANCA ads_management: con questo token si legge ma non si stacca.")
    print("Nel Graph API Explorer: Permissions -> cerca 'ads_management' -> spunta")
    print("-> poi Generate Access Token. Rifai il giro con il token nuovo.")
    raise SystemExit(1)

# 2. allunga a 60 giorni (serve l'app_secret, gia' nel config)
cfg = json.load(open(CFG))
try:
    lungo = get("oauth/access_token", grant_type="fb_exchange_token",
                client_id=cfg["app_id"], client_secret=cfg["app_secret"],
                fb_exchange_token=tok)["access_token"]
    print("token allungato a 60 giorni (poi si rinnova da solo ogni lunedi)")
except Exception as e:
    lungo = tok
    print("non sono riuscito ad allungarlo, uso quello corto:", e)

cfg["access_token"] = lungo
json.dump(cfg, open(CFG, "w"), indent=2)
print("salvato in", CFG)
PY

echo
echo "=== prova dello stacco automatico (simulazione, non tocca niente) ==="
python3 autopause.py
