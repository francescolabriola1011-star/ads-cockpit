"""Client Meta Marketing API — legge account e campagne.

Usa lo stesso token gia' configurato in ~/.config/meta-ads/config.json
(token utente lungo con auto-rinnovo settimanale, vedi skill meta-ads-insights).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://graph.facebook.com/v21.0"
CONFIG = os.path.expanduser("~/.config/meta-ads/config.json")

# action_type che Meta usa per i lead dei moduli istantanei / pixel.
# "lead" e' il canonico e NON va sommato agli altri (sarebbe doppio conteggio).
LEAD_ACTION = "lead"
LEAD_FALLBACKS = (
    "onsite_conversion.lead_grouped",
    "offsite_conversion.fb_pixel_lead",
    "offsite_complete_registration_add_meta_leads",
)


class MetaError(RuntimeError):
    pass


def token() -> str:
    if not os.path.exists(CONFIG):
        raise MetaError(f"config Meta assente: {CONFIG}")
    tok = json.load(open(CONFIG)).get("access_token")
    if not tok:
        raise MetaError("access_token mancante nel config Meta")
    return tok


def _get(path: str, params: dict, tok: str, retries: int = 3) -> dict:
    params = dict(params, access_token=tok)
    url = f"{API}/{path}?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=90) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            # rate limit / errore transitorio -> backoff
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise MetaError(f"HTTP {e.code} su {path}: {body[:300]}") from None
        except Exception as e:  # timeout, rete
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise MetaError(f"errore su {path}: {e}") from None
    raise MetaError(f"fallito {path}")


def list_accounts(tok: str) -> list[dict]:
    out, path, params = [], "me/adaccounts", {
        "fields": "id,account_id,name,account_status,currency",
        "limit": 200,
    }
    d = _get(path, params, tok)
    out.extend(d.get("data", []))
    return out


def campaign_insights(account: str, since: str, until: str, tok: str) -> list[dict]:
    """Insight a livello CAMPAGNA sul periodo. account = 'act_xxx'."""
    params = {
        "level": "campaign",
        "fields": (
            "campaign_id,campaign_name,spend,impressions,clicks,ctr,cpm,"
            "reach,frequency,actions,cost_per_action_type"
        ),
        "time_range": json.dumps({"since": since, "until": until}),
        "limit": 500,
    }
    rows, d = [], _get(f"{account}/insights", params, tok)
    rows.extend(d.get("data", []))
    # paginazione
    nxt = d.get("paging", {}).get("next")
    while nxt:
        with urllib.request.urlopen(nxt, timeout=90) as r:
            d = json.load(r)
        rows.extend(d.get("data", []))
        nxt = d.get("paging", {}).get("next")
    return rows


def campaign_status(account: str, tok: str) -> dict[str, dict]:
    """Stato/budget delle campagne, per capire cosa e' ANCORA ACCESO."""
    params = {
        "fields": "id,name,status,effective_status,daily_budget,lifetime_budget,created_time",
        "limit": 500,
    }
    out: dict[str, dict] = {}
    try:
        d = _get(f"{account}/campaigns", params, tok)
    except MetaError:
        return out
    for c in d.get("data", []):
        out[c["id"]] = c
    nxt = d.get("paging", {}).get("next")
    while nxt:
        try:
            with urllib.request.urlopen(nxt, timeout=90) as r:
                d = json.load(r)
        except Exception:
            break
        for c in d.get("data", []):
            out[c["id"]] = c
        nxt = d.get("paging", {}).get("next")
    return out


def leads_of(row: dict) -> float:
    """Lead della riga. Prende 'lead' se c'e', altrimenti il primo fallback."""
    actions = {a["action_type"]: a.get("value") for a in row.get("actions") or []}
    for key in (LEAD_ACTION,) + LEAD_FALLBACKS:
        if key in actions:
            try:
                return float(actions[key])
            except (TypeError, ValueError):
                return 0.0
    return 0.0
