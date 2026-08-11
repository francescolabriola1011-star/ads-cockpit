#!/usr/bin/env python3
"""ADS COCKPIT — genera i dati per la dashboard.

Legge gli ad account raggiungibili dal token Meta, scarica le campagne del
periodo, applica le regole di casa (config.yaml) e scrive:

  docs/data.json          -> pubblico, nomi cliente in SIGLA
  docs/names.enc          -> nomi VERI (clienti e campagne), cifrati AES-GCM
  clients_private.json    -> mappa sigla -> nome vero in chiaro (NON committato)

La dashboard mostra le sigle a chiunque apra il link; chi conosce la passphrase
sblocca i nomi veri nel browser. La passphrase sta in
~/.config/ads-cockpit/passphrase e non entra mai nel repo.

Uso:  python3 build.py [--account act_xxx] [--out docs/aea] [--titolo "..."] [--no-anon]
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import secrets
import sys
import unicodedata

import yaml
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import meta  # noqa: E402
from rules import Rules  # noqa: E402

ROME = dt.timezone(dt.timedelta(hours=2))


PASSFILE = os.path.expanduser("~/.config/ads-cockpit/passphrase")
STATEFILE = os.path.join(HERE, "state", "known_accounts.json")


def known_accounts() -> set:
    """Account gia' visti nei giri precedenti."""
    try:
        return set(json.load(open(STATEFILE)))
    except Exception:
        return set()


def remember_accounts(ids: set) -> None:
    os.makedirs(os.path.dirname(STATEFILE), exist_ok=True)
    with open(STATEFILE, "w") as f:
        json.dump(sorted(ids), f, indent=1)


def passphrase() -> str:
    """Passphrase per sbloccare i nomi veri. Generata la prima volta e stampata."""
    if os.path.exists(PASSFILE):
        return open(PASSFILE).read().strip()
    os.makedirs(os.path.dirname(PASSFILE), exist_ok=True)
    words = ("oro argento lingotto cockpit lead campagna budget stacco margine "
             "rendita cassa scala portafoglio").split()
    p = "-".join(secrets.choice(words) for _ in range(4)) + "-" + str(secrets.randbelow(900) + 100)
    with open(PASSFILE, "w") as f:
        f.write(p)
    os.chmod(PASSFILE, 0o600)
    print(f"\n*** PASSPHRASE GENERATA (serve a te e ad Alessandro): {p}")
    print(f"*** salvata in {PASSFILE}\n")
    return p


def encrypt_names(clear: dict, pw: str) -> dict:
    """AES-GCM con chiave derivata dalla passphrase (PBKDF2-SHA256, 250k giri)."""
    import hashlib
    salt = secrets.token_bytes(16)
    key = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 250_000, dklen=32)
    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(nonce, json.dumps(clear, ensure_ascii=False).encode(), None)
    b64 = lambda b: base64.b64encode(b).decode()
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": 250_000,
            "salt": b64(salt), "nonce": b64(nonce), "ct": b64(ct)}


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


# Campagne di HIRING/recruiting: NON sono acquisizione clienti, quindi non
# entrano mai in spesa, lead, CPL, sprecato, CAC ne' nella scelta delle creative.
# Riconoscibili dal ruolo cercato in testa al nome: "AM - Moduli", "Setter - Modulo",
# "Venditore - Moduli", "CSM- Moduli", "MB - Moduli", "Editor - Moduli".
HIRING_RE = re.compile(
    r"^\s*(am|mb|csm|setter|venditore|editor|account\s*manager|media\s*buyer)\s*-",
    re.I,
)


def is_hiring(name: str) -> bool:
    return bool(HIRING_RE.match(name or ""))


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
    hiring = []
    for row in rows:
        c = normalize(row, status_map)
        if is_hiring(c["name"]):
            c["hiring"] = True
            hiring.append(c)
            continue
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
    nome = cfg.get("client_names", {}).get(raw_id) or acct.get("name")

    return {
        "alias": alias,
        "account_id": raw_id,
        "nome_reale": nome,
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
        # tenute da parte, mai sommate: servono solo a spiegare il delta col conto Meta
        "hiring": {
            "n": len(hiring),
            "spend": round(sum(c["spend"] for c in hiring), 2),
            "leads": sum(c["leads"] for c in hiring),
        },
        "campagne_hiring": hiring,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", help="solo questo account (act_xxx o id nudo)")
    ap.add_argument("--out", default="docs", help="cartella di output (default docs)")
    ap.add_argument("--titolo", default="ADS Cockpit", help="titolo mostrato in cima")
    ap.add_argument("--sottotitolo", default="tutti i clienti")
    ap.add_argument("--no-anon", action="store_true", help="tieni i nomi veri nel data.json")
    ap.add_argument("--tutti", action="store_true",
                    help="controllo interno: usa le esclusioni della sezione alert, "
                         "quindi tiene dentro anche i clienti nascosti dalla dashboard pubblica")
    args = ap.parse_args()

    cfg = load_config()
    R = Rules(cfg)
    tok = meta.token()

    today = dt.datetime.now(ROME).date()
    since = cfg["period"].get("since") or f"{today.year}-01-01"
    until = today.isoformat()
    recent_since = (today - dt.timedelta(days=int(cfg["period"]["recent_days"]))).isoformat()

    accounts = meta.list_accounts(tok)
    if args.tutti:
        excluded = set(str(x) for x in cfg.get("alert", {}).get("excluded_account_ids", []))
    else:
        excluded = set(str(x) for x in cfg.get("excluded_account_ids", []))
    if args.account:
        want = args.account.replace("act_", "")
        accounts = [a for a in accounts if a["id"].replace("act_", "") == want]
    else:
        accounts = [a for a in accounts if a["id"].replace("act_", "") not in excluded]

    # Clienti NUOVI: entrano da soli appena l'ad account e' raggiungibile dal
    # token, ma vanno segnalati, non scoperti per caso. Il primo giro in
    # assoluto non segnala nulla (sarebbero tutti "nuovi").
    visti = known_accounts()
    ids_ora = {a["id"].replace("act_", "") for a in accounts}
    # Il rilevamento "nuovo cliente" vale solo per il giro standard: con
    # --account o --tutti il perimetro e' diverso e sarebbero falsi positivi.
    giro_standard = not args.account and not args.tutti
    nuovi_ids = (ids_ora - visti) if (visti and giro_standard) else set()
    if giro_standard:
        remember_accounts(visti | ids_ora)

    out, fermi, errors = [], [], []
    for a in accounts:
        label = a.get("name", a["id"])
        raw = a["id"].replace("act_", "")
        try:
            res = build_account(a, cfg, R, tok, since, until, recent_since)
            if res and res["spend"] > 0:
                out.append(res)
                print(f"  ok  {res['alias']:12s} €{res['spend']:>9.2f} "
                      f"{int(res['leads']):>4} lead  sprecato €{res['sprecato']:.2f}")
            else:
                # Cliente collegato ma senza spesa nel periodo: va mostrato lo stesso,
                # perche' "non spende" e' a sua volta un'informazione da vedere.
                alias = cfg.get("aliases", {}).get(raw) or slugify(label)
                fermi.append({"alias": alias, "account_id": raw,
                              "nome_reale": cfg.get("client_names", {}).get(raw) or label})
                print(f"  --  {alias:12s} nessuna spesa nel periodo")
        except Exception as e:
            errors.append({"account": label, "errore": str(e)[:200]})
            print(f"  ERR {label}: {e}", file=sys.stderr)

    out.sort(key=lambda a: -a["sprecato"])

    tot_spend = sum(a["spend"] for a in out)
    tot_leads = sum(a["leads"] for a in out)
    tot_wasted = sum(a["sprecato"] for a in out)
    giorni = max(1, (today - dt.date.fromisoformat(since)).days)

    payload = {
        "titolo": args.titolo,
        "sottotitolo": args.sottotitolo,
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
        "fermi": fermi,
        "nuovi": [a["alias"] for a in out + fermi if a["account_id"] in nuovi_ids],
        "errori": errors,
    }

    anon = cfg.get("anonymize", True) and not args.no_anon

    # I nomi VERI, messi da parte prima di anonimizzare: finiscono cifrati
    # in names.enc e in chiaro solo in clients_private.json (mai committato).
    clear_names = {
        "clienti": {a["alias"]: a["nome_reale"] for a in out + fermi},
        "campagne": {c["id"]: c["name"] for a in out for c in a["campagne"]},
    }
    # La mappa in chiaro si AGGIORNA, non si sovrascrive: ogni build vede solo
    # il suo perimetro e cancellerebbe gli altri clienti.
    privfile = os.path.join(HERE, "clients_private.json")
    try:
        priv = json.load(open(privfile))
    except Exception:
        priv = {}
    priv.update({a["alias"]: {"nome": a["nome_reale"], "account_id": a["account_id"]}
                 for a in out + fermi})
    with open(privfile, "w") as f:
        json.dump(priv, f, indent=2, ensure_ascii=False, sort_keys=True)

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
            for c in a["campagne"] + a.get("campagne_hiring", []):
                c["name"] = re.sub(r"\s{2,}", " ", pat.sub("…", c["name"])).strip()

    if anon:
        for a in payload["clienti"] + payload["fermi"]:
            a.pop("nome_reale", None)
            a.pop("account_id", None)
        for e in payload["errori"]:
            e["account"] = "(account)"

    docs = args.out if os.path.isabs(args.out) else os.path.join(HERE, args.out)
    os.makedirs(docs, exist_ok=True)
    payload["nomi_sbloccabili"] = anon
    with open(os.path.join(docs, "data.json"), "w") as f:
        json.dump(payload, f, indent=1, ensure_ascii=False)

    if anon:
        with open(os.path.join(docs, "names.enc"), "w") as f:
            json.dump(encrypt_names(clear_names, passphrase()), f)

    # la dashboard e' un file solo: le sottocartelle riusano lo stesso index
    idx = os.path.join(HERE, "docs", "index.html")
    if os.path.abspath(docs) != os.path.dirname(idx) and os.path.exists(idx):
        import shutil
        shutil.copy2(idx, os.path.join(docs, "index.html"))

    t = payload["totali"]
    print(f"\nSPESA €{t['spesa']:.2f} | LEAD {int(t['lead'])} | "
          f"CPL €{t['cpl'] or 0:.2f} | SPRECATO €{t['sprecato']:.2f} ({t['quota_sprecata']}%)")
    print(f"Da staccare ORA: {t['da_staccare_ora']} campagne "
          f"(€{t['brucia_oggi']:.2f} al giorno)")
    if payload["nuovi"]:
        print(f"NUOVI CLIENTI ENTRATI: {', '.join(payload['nuovi'])}")
    print(f"-> {os.path.join(docs, 'data.json')}"
          + ("  [nomi anonimizzati]" if anon else "  [NOMI VERI]"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
