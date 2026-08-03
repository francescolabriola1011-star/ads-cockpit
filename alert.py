"""ALERT — il guardiano quotidiano delle regole di run su TUTTI i clienti.

La dashboard mostra tutto a chi la apre. Questo pezzo fa il contrario:
va a cercare Francesco e gli dice solo le cose che vanno decise oggi.

Tre cose e basta:
  1. campagne fuori regola ANCORA ACCESE (soldi che bruciano adesso)
  2. account FERMI o senza lead nella finestra recente (garanzia contatti a rischio)
  3. dove spostare il budget (i vincitori dello stesso account)

Legge il data.json gia' prodotto da build.py: qui non si ricontatta Meta,
cosi' il verdetto e' identico a quello pubblicato sulla dashboard.
I nomi veri restano in locale (il digest non viene mai committato).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
# Dataset del controllo interno: TUTTI i clienti, anche quelli fuori dalla
# dashboard pubblica (build.py --tutti --no-anon --out privato). Mai committato.
DATA = os.path.join(BASE, "privato", "data.json")
DIGEST = os.path.join(BASE, "digest.md")
LINK = "https://francescolabriola1011-star.github.io/ads-cockpit/"


def etichetta(c: dict) -> str:
    """Nome vero + sigla. Il dataset interno gira con --no-anon, i nomi ci sono."""
    nome, alias = c.get("nome_reale"), c.get("alias", "?")
    return f"{nome} ({alias})" if nome and nome != alias else alias


def costruisci(d: dict) -> tuple[str, str]:
    """Ritorna (digest markdown, riga di notifica). Notifica vuota = niente da dire."""
    tot = d["totali"]
    regole = d["regole"]
    giorni = d["periodo"]["finestra_recente_giorni"]

    da_staccare = []      # (cliente, campagna, spend, leads, cpl, motivo, brucia)
    senza_lead = []       # (cliente, lead recenti, spesa recente)
    riallocare = []       # (cliente, budget da spostare, lead prudenti)

    brucia_giorno = 0.0
    for c in d["clienti"]:
        chi = etichetta(c)
        brucia_giorno += c.get("brucia_oggi", 0) or 0
        for k in c["campagne"]:
            if k["status"] == "kill" and k.get("attiva"):
                da_staccare.append((
                    chi, k["name"], k["spend"], k["leads"], k["cpl"],
                    k["reason"], k.get("daily_budget") or 0,
                ))
        r = c.get("riallocazione") or {}
        if r.get("budget_da_spostare", 0) >= 100:
            riallocare.append((chi, r["budget_da_spostare"], r.get("lead_prudente", 0)))

        # garanzia contatti: chi non sta producendo lead nella finestra recente
        rec_lead = sum((k.get("recent") or {}).get("leads", 0) for k in c["campagne"])
        rec_spend = sum((k.get("recent") or {}).get("spend", 0) for k in c["campagne"])
        if any(k.get("attiva") for k in c["campagne"]) and rec_lead == 0:
            senza_lead.append((chi, rec_lead, rec_spend))

    fermi = [etichetta(f) for f in d.get("fermi", [])]

    da_staccare.sort(key=lambda r: -r[2])
    riallocare.sort(key=lambda r: -r[1])

    L = []
    L.append(f"# Run ads — controllo regole · {datetime.now():%d/%m/%Y %H:%M}")
    L.append("")
    L.append(
        f"Regole applicate: CPL sopra €{regole['cpl_kill_eur']:.0f} si stacca · "
        f"0 o 1 lead oltre €{regole['spend_kill_eur']:.0f} spesi si stacca · "
        f"CPL sotto €{regole['cpl_winner_eur']:.0f} si mette budget."
    )
    L.append("")
    L.append(
        f"Periodo {d['periodo']['da']} → {d['periodo']['a']}: "
        f"€{tot['spesa']:,.0f} spesi, {int(tot['lead'])} lead, CPL €{tot['cpl']:.2f}, "
        f"**€{tot['sprecato']:,.0f} sprecati ({tot['quota_sprecata']:.1f}%)**."
    )
    L.append("")

    L.append("## Da staccare ORA")
    if da_staccare:
        L.append(
            f"{len(da_staccare)} campagne fuori regola ancora accese"
            + (f", bruciano €{brucia_giorno:.0f} al giorno." if brucia_giorno else ".")
        )
        L.append("")
        for chi, nome, spend, leads, cpl, motivo, _ in da_staccare:
            cplt = f"CPL €{cpl:.2f}" if cpl else "nessun lead"
            L.append(f"- **{chi}** — {nome}: €{spend:.0f} spesi, {int(leads)} lead, {cplt}. {motivo}")
    else:
        L.append("Nessuna campagna fuori regola accesa. Tutto dentro i parametri.")
    L.append("")

    L.append("## Garanzia contatti a rischio")
    if senza_lead or fermi:
        for chi, _, sp in senza_lead:
            if sp <= 0:
                L.append(f"- **{chi}**: campagne accese ma €0 spesi negli ultimi {giorni} giorni. "
                         "Budget finito o consegna bloccata: la garanzia non avanza.")
            else:
                L.append(f"- **{chi}**: €{sp:.0f} spesi negli ultimi {giorni} giorni e 0 lead. "
                         "Non sta maturando la garanzia.")
        for chi in fermi:
            L.append(f"- **{chi}**: account fermo, nessuna spesa. Se e' in delivery, la garanzia non parte proprio.")
    else:
        L.append("Tutti gli account attivi stanno producendo contatti.")
    L.append("")

    L.append("## Dove spostare il budget")
    if riallocare:
        for chi, budget, lead in riallocare[:6]:
            L.append(f"- **{chi}**: €{budget:,.0f} finiti su campagne da staccare = ~{lead} lead in piu' al CPL limite.")
    else:
        L.append("Niente budget rilevante bloccato sui perdenti.")
    L.append("")
    L.append(f"Dashboard: {LINK}")
    L.append("")

    if da_staccare:
        notifica = f"{len(da_staccare)} campagne da staccare ora"
        if senza_lead or fermi:
            notifica += f" · {len(senza_lead) + len(fermi)} clienti senza contatti"
    elif senza_lead or fermi:
        notifica = f"{len(senza_lead) + len(fermi)} clienti senza contatti negli ultimi {giorni} giorni"
    else:
        notifica = ""

    return "\n".join(L), notifica


def main() -> int:
    if not os.path.exists(DATA):
        print("data.json assente: gira prima build.py", file=sys.stderr)
        return 1
    d = json.load(open(DATA, encoding="utf-8"))
    testo, notifica = costruisci(d)
    open(DIGEST, "w", encoding="utf-8").write(testo)
    print(testo)

    if notifica and "--silenzioso" not in sys.argv:
        subprocess.run([
            "osascript", "-e",
            f'display notification "{notifica}" with title "Run ads — regole" '
            f'subtitle "dettaglio in digest.md" sound name "Basso"',
        ], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
