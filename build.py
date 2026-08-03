#!/usr/bin/env python3
"""ADS COCKPIT — genera docs/data.json per la dashboard.

Legge tutti gli ad account raggiungibili dal token Meta, scarica le campagne
del periodo, applica le regole di casa (config.yaml) e scrive:

  docs/data.json          -> pubblico, nomi cliente ANONIMIZZATI in sigle
  clients_private.json    -> mappa sigla -> nome vero (NON committato)

Uso:  python3 build.py [--account act_xxx] [--no-anon]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import unicodedata

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import meta  # noqa: E402
from rules import Rules  # noqa: E402

ROME = dt.timezone(dt.timedelta(hours=2))


def load_config() -> dict:
    with open(os.path.join(HERE, "config.yaml")) as f:
        return yaml.safe_load(f)


def slugify(name: str) -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", n) if p]
    return ("".join(p[0] for p in parts[:3]) or "ACC").upper()


def num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def normalize(row: dict, status_map: dict) -> dict:
    leads = meta.leads_of(row)
    spend = num(row.get("spend"))
    st = status_map.get(row.get("campaign_id"), {})
    eff = st.get("effective_status", "UNKNOWN")
    return {
        "id": row.get("campaign_id"),
        "name": row.get("campaign_name", "(senza nome)"),
        "spend": round(spend, 2),
        "leads": leads,
        "cpl": round(spend / leads, 2) if leads else None,
        "impressions": num(row.get("impressions")),
        "clicks": num(row.get("clicks")),
        "ctr": num(row.get("ctr")),
        "cpm": num(row.get("cpm")),
        "reach": num(row.get("reach")),
        "frequency": num(row.get("frequency")),
        "attiva": eff in ("ACTIVE", "CAMPAIGN_PAUSED") and eff == "ACTIVE",
        "effective_status": eff,
        "daily_budget": round(num(st.get("daily_budget")) / 100, 2) if st.get("daily_budget") else None,
    }


def build_account(acct: dict, cfg: dict, R: Rules, tok: str, since: str, until: str,
                  recent_since: str) -> dict | None:
    aid = acct["id"]                       # act_xxx
    raw_id = aid.replace("act_", "")

    rows = meta.campaign_insights(aid, since, until, tok)
    if not rows:
        return None
    status_map = meta.campaign_status(aid, tok)

    # finestra recente, per il trend CPL
    recent = {}
    try:
        for r in meta.campaign_insights(aid, recent_since, until, tok):
            recent[r.get("campaign_id")] = r
    except meta.MetaError:
        pass

    campaigns = []
    for row in rows:
        c = normalize(row, status_map)
        status, reason = R.verdict(c)
        c["status"] = status
        c["reason"] = reason
        c["sprecato"] = round(R.wasted(c), 2)
        c["flags"] = R.flags(c)

        rr = recent.get(c["id"])
        if rr:
            rs, rl = num(rr.get("spend")), meta.leads_of(rr)
            c["recent"] = {
                "spend": round(rs, 2),
                "leads": rl,
                "cpl": round(rs / rl, 2) if rl else None,
            }
            if c["cpl"] and c["recent"]["cpl"]:
                c["trend"] = round(c["recent"]["cpl"] - c["cpl"], 2)
            else:
                c["trend"] = None
        else:
            c["recent"] = None
            c["trend"] = None
        campaigns.append(c)

    campaigns.sort(key=lambda c: (-c["sprecato"], -c["spend"]))

    spend = sum(c["spend"] for c in campaigns)
    leads = sum(c["leads"] for c in campaigns)
    sprecato = sum(c["sprecato"] for c in campaigns)
    kill_now = [c for c in campaigns if c["status"] == "kill" and c["attiva"]]

    alias = cfg.get("aliases", {}).get(raw_id) or slugify(acct.get("name", raw_id))

    return {
        "alias": alias,
        "account_id": raw_id,
        "nome_reale": acct.get("name"),
        "currency": acct.get("currency", "EUR"),
        "spend": round(spend, 2),
        "leads": leads,
        "cpl": round(spend / leads, 2) if leads else None,
        "sprecato": round(sprecato, 2),
        "quota_sprecata": round(sprecato / spend * 100, 1) if spend else 0.0,
        "impressions": sum(c["impressions"] for c in campaigns),
        "clicks": sum(c["clicks"] for c in campaigns),
        "n_campagne": len(campaigns),
        "n_kill": len([c for c in campaigns if c["status"] == "kill"]),
        "n_kill_ancora_accese": len(kill_now),
        "brucia_oggi": round(sum(c["daily_budget"] or 0 for c in kill_now), 2),
        "riallocazione": R.reallocation(campaigns),
        "campagne": campaigns,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", help="solo questo account (act_xxx o id nudo)")
    ap.add_argument("--no-anon", action="store_true", help="tieni i nomi veri nel data.json")
    args = ap.parse_args()

    cfg = load_config()
    R = Rules(cfg)
    tok = meta.token()

    today = dt.datetime.now(ROME).date()
    since = cfg["period"].get("since") or f"{today.year}-01-01"
    until = today.isoformat()
    recent_since = (today - dt.timedelta(days=int(cfg["period"]["recent_days"]))).isoformat()

    accounts = meta.list_accounts(tok)
    excluded = set(str(x) for x in cfg.get("excluded_account_ids", []))
    if args.account:
        want = args.account.replace("act_", "")
        accounts = [a for a in accounts if a["id"].replace("act_", "") == want]
    else:
        accounts = [a for a in accounts if a["id"].replace("act_", "") not in excluded]

    out, errors = [], []
    for a in accounts:
        label = a.get("name", a["id"])
        try:
            res = build_account(a, cfg, R, tok, since, until, recent_since)
            if res and res["spend"] > 0:
                out.append(res)
                print(f"  ok  {res['alias']:12s} €{res['spend']:>9.2f} "
                      f"{int(res['leads']):>4} lead  sprecato €{res['sprecato']:.2f}")
            else:
                print(f"  --  {label}: nessuna spesa nel periodo")
        except Exception as e:
            errors.append({"account": label, "errore": str(e)[:200]})
            print(f"  ERR {label}: {e}", file=sys.stderr)

    out.sort(key=lambda a: -a["sprecato"])

    tot_spend = sum(a["spend"] for a in out)
    tot_leads = sum(a["leads"] for a in out)
    tot_wasted = sum(a["sprecato"] for a in out)
    giorni = max(1, (today - dt.date.fromisoformat(since)).days)

    payload = {
        "generato": dt.datetime.now(ROME).isoformat(timespec="seconds"),
        "periodo": {"da": since, "a": until, "giorni": giorni,
                    "finestra_recente_giorni": cfg["period"]["recent_days"]},
        "regole": cfg["rules"],
        "totali": {
            "spesa": round(tot_spend, 2),
            "lead": tot_leads,
            "cpl": round(tot_spend / tot_leads, 2) if tot_leads else None,
            "sprecato": round(tot_wasted, 2),
            "quota_sprecata": round(tot_wasted / tot_spend * 100, 1) if tot_spend else 0.0,
            "sprecato_al_mese": round(tot_wasted / giorni * 30, 2),
            "brucia_oggi": round(sum(a["brucia_oggi"] for a in out), 2),
            "da_staccare_ora": sum(a["n_kill_ancora_accese"] for a in out),
            "clienti": len(out),
            "campagne": sum(a["n_campagne"] for a in out),
        },
        "clienti": out,
        "errori": errors,
    }

    anon = cfg.get("anonymize", True) and not args.no_anon

    # Nomi di persona dentro i nomi campagna: oscurati prima di pubblicare.
    scrub = {t.lower() for t in cfg.get("scrub_terms", [])}
    for a in accounts:
        for w in re.split(r"[^A-Za-zÀ-ÿ]+", a.get("name") or ""):
            if len(w) > 3:
                scrub.add(w.lower())
    scrub -= {"read", "only", "elite", "advisory", "account", "ufficiale",
              "consulente", "assicurativo", "personal"}
    if anon and scrub:
        pat = re.compile(r"\b(" + "|".join(sorted(map(re.escape, scrub), key=len, reverse=True)) + r")\b",
                         re.IGNORECASE)
        for a in payload["clienti"]:
            for c in a["campagne"]:
                c["name"] = re.sub(r"\s{2,}", " ", pat.sub("…", c["name"])).strip()

    priv = {a["alias"]: {"nome": a["nome_reale"], "account_id": a["account_id"]} for a in out}
    with open(os.path.join(HERE, "clients_private.json"), "w") as f:
        json.dump(priv, f, indent=2, ensure_ascii=False)

    if anon:
        for a in payload["clienti"]:
            a.pop("nome_reale", None)
            a.pop("account_id", None)
        for e in payload["errori"]:
            e["account"] = "(account)"

    docs = os.path.join(HERE, "docs")
    os.makedirs(docs, exist_ok=True)
    with open(os.path.join(docs, "data.json"), "w") as f:
        json.dump(payload, f, indent=1, ensure_ascii=False)

    t = payload["totali"]
    print(f"\nSPESA €{t['spesa']:.2f} | LEAD {int(t['lead'])} | "
          f"CPL €{t['cpl'] or 0:.2f} | SPRECATO €{t['sprecato']:.2f} ({t['quota_sprecata']}%)")
    print(f"Da staccare ORA: {t['da_staccare_ora']} campagne "
          f"(€{t['brucia_oggi']:.2f} al giorno)")
    print(f"-> {os.path.join(docs, 'data.json')}"
          + ("  [nomi anonimizzati]" if anon else "  [NOMI VERI]"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
