#!/usr/bin/env python3
"""STACCO AUTOMATICO delle campagne fuori regola.

Regole (config.yaml -> autopause.rules):
  - 0 lead e spesa oltre  €15  -> in pausa
  - 1 lead e spesa oltre  €22  -> in pausa

Vincoli di sicurezza, volutamente stretti:
  - agisce sugli account di autopause.account_ids. Con "auto" copre tutti i
    clienti della dashboard generale (quindi anche i nuovi appena entrano)
    piu' quelli di account_ids_extra. Gli id in mai_toccare restano esclusi
    sempre.
  - tocca solo campagne ACTIVE, e le mette in PAUSED (reversibile, non elimina).
  - senza --esegui non scrive niente: stampa solo cosa farebbe.
  - ogni azione finisce in logs/autopause.log e in docs/aea/staccate.json,
    così sulla dashboard si vede cosa ha staccato e quando.

Uso:
  python3 autopause.py            # simulazione, non tocca nulla
  python3 autopause.py --esegui   # mette davvero in pausa
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import meta  # noqa: E402

ROME = dt.timezone(dt.timedelta(hours=2))
LOG = os.path.join(HERE, "logs", "autopause.log")


def num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def pausa(campaign_id: str, tok: str) -> tuple[bool, str]:
    """Mette la campagna in PAUSED. Serve il permesso ads_management."""
    data = urllib.parse.urlencode({"status": "PAUSED", "access_token": tok}).encode()
    req = urllib.request.Request(f"{meta.API}/{campaign_id}", data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return True, json.load(r).get("success", "ok") and "ok"
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(body)["error"]["message"]
        except Exception:
            msg = body[:200]
        return False, msg
    except Exception as e:
        return False, str(e)


def log(riga: str) -> None:
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(f"{dt.datetime.now(ROME).isoformat(timespec='seconds')}  {riga}\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--esegui", action="store_true",
                    help="mette davvero in pausa (senza, e' solo simulazione)")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(os.path.join(HERE, "config.yaml")))
    ap_cfg = cfg.get("autopause") or {}
    if not ap_cfg.get("attivo"):
        print("autopause: spento in config.yaml (autopause.attivo: false)")
        return 0

    r = ap_cfg["rules"]
    kill_no_lead = num(r["spesa_zero_lead_eur"])
    kill_un_lead = num(r["spesa_un_lead_eur"])
    cpl_max = num(r.get("cpl_max_eur")) if r.get("cpl_max_eur") else None
    ids = ap_cfg.get("account_ids")
    if ids == "auto":
        # tutti gli account della dashboard generale: i clienti nuovi entrano
        # nell'automazione da soli, senza toccare la configurazione
        fuori = set(str(x) for x in cfg.get("excluded_account_ids", []))
        accounts = [a["id"].replace("act_", "") for a in meta.list_accounts(meta.token())
                    if a["id"].replace("act_", "") not in fuori]
    else:
        accounts = [str(a) for a in (ids or [])]
    accounts += [str(a) for a in ap_cfg.get("account_ids_extra", [])]
    mai = set(str(a) for a in ap_cfg.get("mai_toccare", []))
    accounts = [a for a in dict.fromkeys(accounts) if a not in mai]
    if not accounts:
        print("autopause: nessun account autorizzato, non faccio niente")
        return 0

    tok = meta.token()
    today = dt.datetime.now(ROME).date()
    since = cfg["period"].get("since") or f"{today.year}-01-01"

    modo = "ESECUZIONE" if args.esegui else "SIMULAZIONE"
    print(f"=== Stacco automatico [{modo}] ===")
    print(f"Regole: 0 lead oltre €{kill_no_lead:.0f} · 1 lead oltre €{kill_un_lead:.0f}"
          + (f" · CPL sopra €{cpl_max:.0f}" if cpl_max else " · (CPL non automatizzato)"))

    staccate, errori = [], []
    for raw in accounts:
        acct = f"act_{raw}"
        rows = meta.campaign_insights(acct, since, today.isoformat(), tok)
        stato = meta.campaign_status(acct, tok)

        for row in rows:
            cid = row.get("campaign_id")
            st = stato.get(cid, {})
            if st.get("effective_status") != "ACTIVE":
                continue                      # gia' spenta, niente da fare

            spend, leads = num(row.get("spend")), meta.leads_of(row)
            motivo = None
            if leads == 0 and spend > kill_no_lead:
                motivo = f"€{spend:.2f} spesi e 0 lead (limite €{kill_no_lead:.0f})"
            elif leads == 1 and spend > kill_un_lead:
                motivo = f"€{spend:.2f} spesi per 1 solo lead (limite €{kill_un_lead:.0f})"
            elif cpl_max and leads > 0 and spend / leads > cpl_max:
                motivo = f"CPL €{spend / leads:.2f} sopra il limite di €{cpl_max:.0f}"
            if not motivo:
                continue

            nome = row.get("campaign_name", cid)
            voce = {"quando": dt.datetime.now(ROME).isoformat(timespec="seconds"),
                    "account": raw, "campagna": nome, "campaign_id": cid,
                    "motivo": motivo, "spesa": round(spend, 2), "lead": leads,
                    "eseguito": False}

            if args.esegui:
                ok, msg = pausa(cid, tok)
                voce["eseguito"] = ok
                if ok:
                    print(f"  STACCATA  {nome} · {motivo}")
                    log(f"PAUSED {cid} «{nome}» {motivo}")
                else:
                    voce["errore"] = msg
                    errori.append((nome, msg))
                    print(f"  ERRORE    {nome}: {msg}")
                    log(f"ERRORE {cid} «{nome}»: {msg}")
            else:
                print(f"  da staccare  {nome} · {motivo}")
            staccate.append(voce)

    # storico per la dashboard
    if args.esegui and staccate:
        p = os.path.join(HERE, "docs", "aea", "staccate.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        try:
            storico = json.load(open(p))
        except Exception:
            storico = []
        storico = ([v for v in staccate if v["eseguito"]] + storico)[:50]
        with open(p, "w") as f:
            json.dump(storico, f, indent=1, ensure_ascii=False)

    if not staccate:
        print("  niente da staccare: tutte le campagne accese sono dentro le regole")
    elif not args.esegui:
        print(f"\n{len(staccate)} campagne da staccare. "
              f"Per farlo davvero: python3 autopause.py --esegui")
    if errori:
        print(f"\n{len(errori)} errori. Se dicono 'permission', al token manca "
              f"ads_management: va rigenerato con quel permesso.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
