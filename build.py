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


def metrics(row: dict) -> dict:
    """Numeri comuni a campagna e inserzione, da una riga di insight Meta."""
    leads = meta.leads_of(row)
    spend = num(row.get("spend"))
    clicks = num(row.get("clicks"))
    return {
        "spend": round(spend, 2),
        "leads": leads,
        "cpl": round(spend / leads, 2) if leads else None,
        "impressions": num(row.get("impressions")),
        "clicks": clicks,
        # CVR click->lead: dice se il collo e' il MODULO invece delle ads
        "cvr": round(leads / clicks * 100, 2) if clicks else None,
        "ctr": num(row.get("ctr")),
        "cpm": num(row.get("cpm")),
        "reach": num(row.get("reach")),
        "frequency": num(row.get("frequency")),
    }


def normalize(row: dict, status_map: dict) -> dict:
    st = status_map.get(row.get("campaign_id"), {})
    eff = st.get("effective_status", "UNKNOWN")
    return {
        "id": row.get("campaign_id"),
        "name": row.get("campaign_name", "(senza nome)"),
        **metrics(row),
        "attiva": eff == "ACTIVE",
        "effective_status": eff,
        "daily_budget": round(num(st.get("daily_budget")) / 100, 2) if st.get("daily_budget") else None,
    }


def normalize_ad(row: dict, ad_status: dict) -> dict:
    st = ad_status.get(row.get("ad_id"), {})
    # ACTIVE solo se gira davvero: con adset o campagna spenti Meta da'
    # ADSET_PAUSED / CAMPAIGN_PAUSED anche se l'inserzione in se' e' accesa.
    eff = st.get("effective_status", "UNKNOWN")
    return {
        "id": row.get("ad_id"),
        "name": row.get("ad_name", "(senza nome)"),
        "campaign_id": row.get("campaign_id"),
        "campaign_name": row.get("campaign_name", "(senza nome)"),
        **metrics(row),
        "attiva": eff == "ACTIVE",
        "effective_status": eff,
    }


def with_recent(x: dict, rr: dict | None) -> None:
    """CPL della finestra recente e trend rispetto allo storico."""
    if not rr:
        x["recent"] = None
        x["trend"] = None
        return
    rs, rl = num(rr.get("spend")), meta.leads_of(rr)
    x["recent"] = {"spend": round(rs, 2), "leads": rl, "cpl": round(rs / rl, 2) if rl else None}
    x["trend"] = round(x["recent"]["cpl"] - x["cpl"], 2) if x["cpl"] and x["recent"]["cpl"] else None


def vivo_of(ads: list[dict]) -> dict | None:
    """Le sole creative ANCORA ACCESE: e' su queste che si legge CPL/CVR/CTR
    attuali. La media di campagna include le creative gia' staccate e racconta
    un CPL che non esiste piu'. None se non c'e' nessuna creativa accesa."""
    on = [a for a in ads if a["attiva"]]
    if not on:
        return None
    spend = sum(a["spend"] for a in on)
    leads = sum(a["leads"] for a in on)
    clicks = sum(a["clicks"] for a in on)
    impr = sum(a["impressions"] for a in on)
    r_spend = sum((a.get("recent") or {}).get("spend", 0) for a in on)
    r_leads = sum((a.get("recent") or {}).get("leads", 0) for a in on)
    return {
        "spend": round(spend, 2),
        "leads": leads,
        "clicks": clicks,
        "impressions": impr,
        "cpl": round(spend / leads, 2) if leads else None,
        "cvr": round(leads / clicks * 100, 2) if clicks else None,
        "ctr": round(clicks / impr * 100, 2) if impr else None,
        "recent_cpl": round(r_spend / r_leads, 2) if r_leads else None,
        "recent_spend": round(r_spend, 2),
        "recent_leads": r_leads,
    }


def verdict_from_ads(ads: list[dict]) -> tuple[str, str, float]:
    """Stato, motivo e sprecato della CAMPAGNA letti sulle creative accese.

    Una creativa gia' staccata non e' piu' un'azione da fare: non rende la
    campagna "da staccare" e il suo sprecato e' storia, non budget che brucia.
    """
    on = [a for a in ads if a["attiva"]]
    kill_on = [a for a in on if a["status"] == "kill"]
    win_on = [a for a in on if a["status"] == "winner"]
    sprecato = round(sum(a["sprecato"] for a in on), 2)
    if kill_on:
        worst = max(kill_on, key=lambda a: a["sprecato"])
        return "kill", (f"{len(kill_on)} inserzione/i ancora accesa/e fuori soglia "
                        f"(la peggiore: {worst['name']}, {worst['reason']})"), sprecato
    if win_on:
        return "winner", f"{len(win_on)} inserzione/i vincente/i ancora accesa/e", sprecato
    if on:
        return "ok", "inserzioni accese dentro i parametri", sprecato
    if any(a["status"] == "kill" for a in ads):
        return "ok", "tutte le inserzioni fuori soglia sono gia' state staccate", sprecato
    return "ok", "nessuna inserzione accesa", sprecato


def build_account(acct: dict, cfg: dict, R: Rules, tok: str, since: str, until: str,
                  recent_since: str) -> dict | None:
    aid = acct["id"]                       # act_xxx
    raw_id = aid.replace("act_", "")

    rows = meta.campaign_insights(aid, since, until, tok)
    if not rows:
        return None
    status_map = meta.campaign_status(aid, tok)

    # Livello CREATIVITA': una riga per inserzione, riagganciata alla campagna.
    ad_rows = meta.ad_insights(aid, since, until, tok)
    ad_status = meta.ad_status(aid, tok)

    # finestra recente, per il trend CPL (campagna e creativita')
    recent, recent_ads = {}, {}
    try:
        for r in meta.campaign_insights(aid, recent_since, until, tok):
            recent[r.get("campaign_id")] = r
        for r in meta.ad_insights(aid, recent_since, until, tok):
            recent_ads[r.get("ad_id")] = r
    except meta.MetaError:
        pass

    ads_by_camp: dict[str, list[dict]] = {}
    for row in ad_rows:
        a = normalize_ad(row, ad_status)
        status, reason = R.verdict(a)
        a["status"] = status
        a["reason"] = reason
        a["sprecato"] = round(R.wasted(a), 2)
        a["flags"] = R.flags(a)
        with_recent(a, recent_ads.get(a["id"]))
        ads_by_camp.setdefault(a["campaign_id"], []).append(a)

    campaigns = []
    hiring = []
    for row in rows:
        c = normalize(row, status_map)
        if is_hiring(c["name"]):
            c["hiring"] = True
            hiring.append(c)
            continue
        ads = sorted(ads_by_camp.get(c["id"], []),
                     key=lambda a: (not a["attiva"], -a["spend"]))
        c["ads"] = ads
        c["n_ads"] = len(ads)
        c["n_ads_attivi"] = sum(1 for a in ads if a["attiva"])
        c["vivo"] = vivo_of(ads)
        # Senza almeno uno stato letto non si puo' sapere cosa e' acceso: il
        # verdetto di campagna e' meglio che dichiarare le creative "gia' staccate".
        stati_noti = any(a.get("effective_status") not in (None, "", "UNKNOWN") for a in ads)
        if ads and stati_noti:
            c["status"], c["reason"], c["sprecato"] = verdict_from_ads(ads)
        else:
            c["status"], c["reason"] = R.verdict(c)
            c["sprecato"] = round(R.wasted(c), 2)
        c["flags"] = R.flags(c)
        with_recent(c, recent.get(c["id"]))
        campaigns.append(c)

    campaigns.sort(key=lambda c: (-c["sprecato"], -c["spend"]))

    spend = sum(c["spend"] for c in campaigns)
    leads = sum(c["leads"] for c in campaigns)
    sprecato = sum(c["sprecato"] for c in campaigns)
    kill_now = [c for c in campaigns if c["status"] == "kill" and c["attiva"]]

    # creative accese di tutto l'account
    vivi = [c["vivo"] for c in campaigns if c["vivo"]]
    v_spend = sum(v["spend"] for v in vivi)
    v_leads = sum(v["leads"] for v in vivi)
    v_clicks = sum(v["clicks"] for v in vivi)
    v_rs = sum(v["recent_spend"] for v in vivi)
    v_rl = sum(v["recent_leads"] for v in vivi)
    vivo = {
        "spend": round(v_spend, 2),
        "leads": v_leads,
        "cpl": round(v_spend / v_leads, 2) if v_leads else None,
        "cvr": round(v_leads / v_clicks * 100, 2) if v_clicks else None,
        "recent_cpl": round(v_rs / v_rl, 2) if v_rl else None,
        "n_creative": sum(c["n_ads_attivi"] for c in campaigns),
    } if vivi else None

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
        "cvr": (round(leads / sum(c["clicks"] for c in campaigns) * 100, 2)
                if sum(c["clicks"] for c in campaigns) else None),
        "n_campagne": len(campaigns),
        "n_kill": len([c for c in campaigns if c["status"] == "kill"]),
        "n_kill_ancora_accese": len(kill_now),
        "brucia_oggi": round(sum(c["daily_budget"] or 0 for c in kill_now), 2),
        "vivo": vivo,
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


# Parole dei nomi account che NON identificano una persona (e il nome di casa
# "AI Elite Advisory", che compare anche nel titolo della dashboard aea).
NON_NOMI = {"read", "only", "elite", "advisory", "account", "ufficiale",
            "consulente", "assicurativo", "personal"}


def identifica(nome: str | None) -> bool:
    """True se il nome contiene almeno una parola che puo' identificare qualcuno."""
    return any(len(w) > 3 and w.lower() not in NON_NOMI
               for w in re.split(r"[^A-Za-zÀ-ÿ]+", nome or ""))


def is_public(path: str) -> bool:
    """True se la cartella sta sotto docs/, cioe' viene pubblicata su GitHub Pages."""
    pub = os.path.realpath(os.path.join(HERE, "docs"))
    p = os.path.realpath(path)
    return p == pub or p.startswith(pub + os.sep)


def leaks(payload: dict, nomi: set, ids: set) -> list[str]:
    """Ultimo controllo prima di scrivere un data.json anonimo: nessun nome vero
    di cliente e nessun id di ad account deve comparire, in nessun campo."""
    testo = json.dumps(payload, ensure_ascii=False).lower()
    trovati = []
    if any(a.get("nome_reale") not in (None, a.get("alias"))
           for a in payload["clienti"] + payload["fermi"]):
        trovati.append("nome_reale diverso dalla sigla")
    if '"account_id"' in testo:
        trovati.append("campo account_id")
    if any(i and re.search(r"(?<!\d)" + re.escape(i) + r"(?!\d)", testo) for i in ids):
        trovati.append("id di ad account")
    if any(identifica(n) and n.lower() in testo for n in nomi):
        trovati.append("nome vero di un cliente")
    return trovati


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
            "clicks": sum(a["clicks"] for a in out),
            "cvr": (round(tot_leads / sum(a["clicks"] for a in out) * 100, 2)
                    if sum(a["clicks"] for a in out) else None),
            "clienti": len(out),
            "campagne": sum(a["n_campagne"] for a in out),
        },
        "clienti": out,
        "fermi": fermi,
        "nuovi": [a["alias"] for a in out + fermi if a["account_id"] in nuovi_ids],
        "errori": errors,
    }

    docs = args.out if os.path.isabs(args.out) else os.path.join(HERE, args.out)
    # Tutto cio' che sta sotto docs/ finisce su GitHub Pages PUBBLICO: li'
    # l'anonimizzazione e' obbligatoria, ne' --no-anon ne' anonymize:false
    # possono spegnerla. I nomi veri in chiaro solo fuori da docs/ (es. privato/).
    pubblico = is_public(docs)
    anon = (cfg.get("anonymize", True) and not args.no_anon) or pubblico
    if pubblico and (args.no_anon or not cfg.get("anonymize", True)):
        print(f"  ATTENZIONE: {docs} e' pubblicato, nomi veri ignorati: "
              "per i nomi in chiaro usa --out privato", file=sys.stderr)

    # I nomi VERI, messi da parte prima di anonimizzare: finiscono cifrati
    # in names.enc e in chiaro solo in clients_private.json (mai committato).
    clear_names = {
        "clienti": {a["alias"]: a["nome_reale"] for a in out + fermi},
        "campagne": {c["id"]: c["name"] for a in out for c in a["campagne"]},
        "inserzioni": {ad["id"]: ad["name"] for a in out for c in a["campagne"]
                       for ad in c.get("ads", [])},
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

    # Nomi di persona dentro i nomi di campagne e creative: oscurati prima di pubblicare.
    scrub = {t.lower() for t in cfg.get("scrub_terms", [])}
    for a in accounts:
        for w in re.split(r"[^A-Za-zÀ-ÿ]+", a.get("name") or ""):
            if len(w) > 3:
                scrub.add(w.lower())
    for a in out + fermi:
        for w in re.split(r"[^A-Za-zÀ-ÿ]+", a.get("nome_reale") or ""):
            if len(w) > 3:
                scrub.add(w.lower())
    scrub -= NON_NOMI
    if anon and scrub:
        pat = re.compile(r"\b(" + "|".join(sorted(map(re.escape, scrub), key=len, reverse=True)) + r")\b",
                         re.IGNORECASE)
        clean = lambda t: re.sub(r"\s{2,}", " ", pat.sub("…", t or "")).strip()
        for a in payload["clienti"]:
            for c in a["campagne"] + a.get("campagne_hiring", []):
                c["name"] = clean(c["name"])
                if c.get("reason"):
                    c["reason"] = clean(c["reason"])  # cita il nome della creativa peggiore
                if c.get("flags"):
                    c["flags"] = [clean(f) for f in c["flags"]]
                for ad in c.get("ads", []):
                    ad["name"] = clean(ad["name"])
                    ad["campaign_name"] = clean(ad["campaign_name"])
                    if ad.get("reason"):
                        ad["reason"] = clean(ad["reason"])
                    if ad.get("flags"):
                        ad["flags"] = [clean(f) for f in ad["flags"]]

    # nomi e id veri messi da parte PRIMA di anonimizzare (gli oggetti del
    # payload sono gli stessi di out/fermi): servono al controllo finale
    nomi_veri = {a.get("nome_reale") for a in out + fermi} | {a.get("name") for a in accounts}
    ids_veri = {a["id"].replace("act_", "") for a in accounts} | {a["account_id"] for a in out + fermi}

    if anon:
        # nome_reale resta come campo ma porta solo la sigla: il nome vero
        # si legge solo sbloccando names.enc con la passphrase.
        for a in payload["clienti"] + payload["fermi"]:
            a["nome_reale"] = a["alias"]
            a.pop("account_id", None)
        for e in payload["errori"]:
            e["account"] = "(account)"
            e["errore"] = "(dettaglio nel log)"

    os.makedirs(docs, exist_ok=True)
    payload["nomi_sbloccabili"] = anon
    if anon:
        trapelati = leaks(payload, nomi_veri, ids_veri)
        if trapelati:
            print(f"STOP: data.json conterrebbe dati in chiaro ({', '.join(trapelati)}), "
                  "non scritto", file=sys.stderr)
            return 2
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
