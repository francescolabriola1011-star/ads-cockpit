"""Le regole di casa applicate alle campagne + calcolo del budget sprecato.

Regole (da config.yaml):
  1. Campagna con 0 lead: si stacca oltre spend_kill_zero_lead_eur (€30).
  1b. Campagna con 1 lead: si guarda SOLO la spesa, si stacca oltre spend_kill_eur (€50)
     (con un lead solo il CPL non e' un dato, e' un caso: NON si applica cpl_kill_eur)
  2. Campagna con 2 lead o piu' e CPL > cpl_kill_eur -> STACCA

Il CHECKPOINT dei €50 (spend_kill_eur), che e' quello che conta davvero:
entro quella spesa la campagna deve aver portato ALMENO 2 lead, cioe' un CPL sotto
€25 (50 / 2). Quindi: sotto €50 si lascia respirare (1 lead a €40 NON si stacca,
altrimenti non si trova mai una vincente), ma arrivati a €50 il secondo lead dev'esserci.

Soglie UFFICIALI dal 20/8/2026 (superano le 15/22 del 17/8). ATTENZIONE, incoerenza
nota e non ancora decisa: cpl_kill_eur e' rimasto 22, quindi una campagna con 2 lead a
€46 (CPL 23) viene staccata mentre una con 1 lead a €49 sopravvive, cioe' il righello
punisce quella che va meglio.
  3. Campagna con 0 lead e spesa >= spend_warn_eur    -> SORVEGLIATA
  4. Campagna con CPL <= cpl_winner_eur               -> VINCITRICE (ci si mette budget)

Budget sprecato = euro usciti OLTRE il punto in cui la regola diceva di staccare.
  - campagna con 0 lead: speso oltre spend_kill_zero_lead_eur
  - campagna con 1 lead: speso oltre spend_kill_eur
  - campagna con 2+ lead e CPL troppo alto: spesa - (lead x cpl_kill_eur)
"""
from __future__ import annotations


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class Rules:
    def __init__(self, cfg: dict):
        r = cfg["rules"]
        self.cpl_kill = _f(r["cpl_kill_eur"])
        self.spend_kill = _f(r["spend_kill_eur"])
        # 0 lead: limite piu' severo, senza nemmeno un contatto non si aspetta €22
        self.spend_kill_zero = _f(r.get("spend_kill_zero_lead_eur") or r["spend_warn_eur"])
        self.spend_warn = _f(r["spend_warn_eur"])
        self.cpl_winner = _f(r["cpl_winner_eur"])
        self.ctr_floor = _f(r["ctr_floor_pct"])
        self.freq_ceiling = _f(r["frequency_ceiling"])
        self.min_impr = _f(r["min_impressions"])
        # Campagne staccate a torto, da tenere sott'occhio: vengono dal config,
        # cosi' la lista si cambia senza toccare il codice.
        self.riprova = {str(x).strip().lower()
                        for x in (cfg.get("riprova_candidate") or [])}
        # CVR click->lead: sotto la soglia il collo e' il modulo, non le ads (20/8/2026)
        self.cvr_floor = _f(r.get("cvr_floor_pct") or 4.0)
        self.cvr_min_clicks = _f(r.get("cvr_min_clicks") or 30)

    # ---------------- verdetto ----------------
    def verdict(self, c: dict) -> tuple[str, str]:
        """Ritorna (stato, motivo). Stato: kill | watch | winner | ok | early."""
        spend, leads, impr = c["spend"], c["leads"], c["impressions"]
        cpl = c["cpl"]

        if impr < self.min_impr and spend < self.spend_kill_zero:
            return "early", f"solo {int(impr)} impression: troppo presto per giudicare"

        if leads == 0:
            if spend > self.spend_kill_zero:
                return "kill", (
                    f"€{spend:.2f} spesi e 0 lead: oltre il limite di €{self.spend_kill_zero:.0f}"
                )
            return "watch", (
                f"€{spend:.2f} spesi e 0 lead: mancano "
                f"€{self.spend_kill_zero - spend:.2f} allo stacco"
            )

        if leads <= 1:
            # Con un lead solo il CPL non e' un dato attendibile: conta solo la spesa.
            if spend > self.spend_kill:
                return "kill", (
                    f"1 solo lead a €{spend:.2f}: oltre il limite di €{self.spend_kill:.0f}"
                )
            return "early", (
                f"1 lead a €{spend:.2f}: si lascia girare, ma entro €{self.spend_kill:.0f} "
                f"(mancano €{self.spend_kill - spend:.2f}) deve arrivare il 2° lead, "
                f"cioe' CPL sotto €{self.spend_kill / 2:.0f}"
            )

        if cpl is not None and cpl > self.cpl_kill:
            return "kill", f"CPL €{cpl:.2f}: sopra il limite di €{self.cpl_kill:.0f}"

        if cpl is not None and cpl <= self.cpl_winner:
            return "winner", f"CPL €{cpl:.2f}: sotto €{self.cpl_winner:.0f}, ci si mette budget"

        return "ok", f"CPL €{cpl:.2f}: dentro i parametri"

    # ---------------- budget sprecato ----------------
    def wasted(self, c: dict) -> float:
        spend, leads, cpl = c["spend"], c["leads"], c["cpl"]
        if leads == 0:
            return max(0.0, spend - self.spend_kill_zero)
        if leads == 1:
            return max(0.0, spend - self.spend_kill)
        if cpl is not None and cpl > self.cpl_kill:
            return max(0.0, spend - leads * self.cpl_kill)
        return 0.0

    # ---------------- segnali di qualita' ----------------
    def flags(self, c: dict) -> list[str]:
        out = []
        if c["impressions"] >= self.min_impr:
            if 0 < c["ctr"] < self.ctr_floor:
                out.append(f"CTR {c['ctr']:.2f}%: il creativo non ferma nessuno")
            if c["frequency"] > self.freq_ceiling:
                out.append(f"frequenza {c['frequency']:.1f}: pubblico bruciato, cambia audience")
            if c["cpm"] > 60:
                out.append(f"CPM €{c['cpm']:.0f}: stai pagando caro il posizionamento")
        if c["clicks"] >= self.cvr_min_clicks:
            cvr = c["leads"] / c["clicks"] * 100
            if cvr < self.cvr_floor:
                out.append(f"solo {cvr:.1f}% dei click diventa lead: modulo o landing da rivedere")
        if c["spend"] > 0 and c["impressions"] == 0:
            out.append("spesa senza impression: campagna in errore, controlla subito")
        nome = str(c.get("name") or c.get("nome") or "").strip().lower()
        if nome and nome in self.riprova:
            out.append("DA RIPROVARE: staccata a torto, aveva campione e CPL sotto target")
        return out

    # ---------------- riallocazione ----------------
    def reallocation(self, campaigns: list[dict]) -> dict:
        """Quanto budget e' finito sui perdenti e quanti lead darebbe altrove.

        Due stime, perche' una sola sarebbe una promessa:
          - PRUDENTE: lo stesso budget speso al CPL limite (€22, cpl_kill_eur). E' il minimo
            sindacale che ci si aspetta da una campagna che resta accesa.
          - OTTIMISTICA: al CPL delle campagne vincenti dello stesso account.
            Vale solo se quelle campagne reggono lo scale, quindi si guarda
            ma non ci si scommette sopra.
        """
        losing = sum(c["spend"] for c in campaigns if c["status"] == "kill")
        winners = [c for c in campaigns if c["status"] == "winner"]
        w_spend = sum(c["spend"] for c in winners)
        w_leads = sum(c["leads"] for c in winners)
        cpl_w = w_spend / w_leads if w_leads else None
        return {
            "budget_da_spostare": round(losing, 2),
            "cpl_vincente": round(cpl_w, 2) if cpl_w else None,
            "lead_prudente": int(losing / self.cpl_kill) if self.cpl_kill else 0,
            "lead_ottimistico": int(losing / cpl_w) if cpl_w else 0,
        }
