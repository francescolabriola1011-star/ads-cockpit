# ADS Cockpit

Dashboard unica per fare il **run delle ads di tutti i clienti** con le regole di casa,
senza aprire venti Gestioni Inserzioni.

**Dashboard generale** (tutti gli account, con selettore):
https://francescolabriola1011-star.github.io/ads-cockpit/

**Dashboard AI Elite Advisory** (solo casa nostra):
https://francescolabriola1011-star.github.io/ads-cockpit/aea/

**Link del singolo cliente**: basta aggiungere `?cliente=SIGLA`, es.
`.../ads-cockpit/?cliente=CF-SM`. La pagina si apre già scopata su quel solo account:
il suo sprecato, le sue campagne da staccare, le sue vincenti. È il link da mandare
al cliente.

## Le regole applicate

| Regola | Verdetto |
|---|---|
| CPL sopra **€15** | STACCA |
| 0 o 1 lead con più di **€22** spesi | STACCA |
| 0 lead da **€15** in su | SORVEGLIA |
| CPL sotto **€8** | VINCENTE, ci si mette budget |

Si cambiano in [config.yaml](config.yaml), sezione `rules`. Il codice non si tocca.

## Budget sprecato

Non è "quanto abbiamo speso male a occhio": è **quanto è uscito oltre il punto in cui
la regola diceva di staccare**.

- campagna senza lead → tutto lo speso oltre €22
- campagna con lead ma CPL troppo alto → `spesa − (lead × €15)`

Sommato per cliente e in totale, più la proiezione a 30 giorni al ritmo attuale.

## Cosa mostra

1. **Il quadro** — spesa, lead, CPL medio, sprecato totale e proiezione mensile, lead mancati.
2. **Azioni di oggi** — solo le campagne fuori regola **ancora accese**, ordinate per quanto
   bruciano al giorno. È la lista da eseguire, non da leggere.
3. **Dove riallocare** — budget parcheggiato sui perdenti per cliente, con stima prudente
   dei lead mancati e CPL delle campagne vincenti.
4. **Clienti** — una riga per cliente, apribile sul dettaglio campagna per campagna con
   stato, CPL, CPL degli ultimi 7 giorni (trend), CTR e sprecato.

Oltre alle soglie, ogni campagna viene marcata con i segnali che spiegano *perché* va male:
CTR sotto l'1% (creativo che non ferma), frequenza sopra 2.5 (pubblico bruciato),
CPM sopra €60, meno del 5% dei click che diventa lead (modulo o landing da rivedere),
spesa senza impression (campagna in errore).

## Privacy e nomi veri

Il sito è pubblico, quindi di default si vedono solo le **sigle** (`CF-SM`, `ORO-GMY`),
e anche i nomi di persona dentro i nomi campagna sono oscurati.

I nomi veri esistono comunque, ma **cifrati**: `docs/names.enc` è un blob AES-GCM con
chiave derivata da una passphrase (PBKDF2-SHA256, 250.000 giri). Il bottone
**"Mostra nomi veri"** chiede la passphrase e decifra nel browser: da quel momento
clienti e campagne compaiono col nome vero, per tutta la sessione.

- La passphrase sta in `~/.config/ads-cockpit/passphrase` (mai nel repo).
- Senza passphrase, chi apre il link vede solo sigle e non può risalire a nulla.
- I nomi puliti si scrivono in `config.yaml` → `client_names`.
- `clients_private.json` ha la stessa mappa in chiaro, in locale, per comodità.

## Uso

```bash
./run.sh                        # tutti i clienti, poi push su Pages
python3 build.py                # solo rigenera i dati, senza push
python3 build.py --account act_833628578375108   # un cliente solo
python3 build.py --no-anon      # nomi veri (SOLO per guardare in locale, mai da pushare)
```

Dashboard in locale:

```bash
cd docs && python3 -m http.server 8791    # poi http://localhost:8791
```

## Aggiungere un cliente nuovo

Niente da fare: appena l'ad account è collegato al Business Manager, lo script lo trova da solo
al giro dopo. Al massimo gli si dà una sigla parlante in `config.yaml` → `aliases`.
Per escluderne uno, il suo id va in `excluded_account_ids`.

## Automazione

`com.clienti10x.adscockpit` (launchd) lo lancia **ogni mattina alle 08:00**: legge Meta,
ricalcola, committa e pusha. Log in `logs/`.

```bash
launchctl list | grep adscockpit          # è attivo?
tail -f logs/run.log                      # cosa ha fatto stamattina
```

## Dati

Meta Marketing API letta direttamente (niente Windsor). Usa il token in
`~/.config/meta-ads/config.json`, quello con auto rinnovo settimanale della skill
`meta-ads-insights`. Se la dashboard smette di aggiornarsi, il primo sospetto è il token.

## Setup su un altro Mac (es. Alessandro)

```bash
git clone https://github.com/francescolabriola1011-star/ads-cockpit.git
cd ads-cockpit
cp config.example.yaml config.yaml      # poi ci metti account esclusi e sigle
```

Serve il token Meta in `~/.config/meta-ads/config.json`:

```json
{ "access_token": "...", "app_id": "...", "app_secret": "..." }
```

Il token va generato dal Graph API Explorer con permesso `ads_read`, con un utente
che ha accesso agli ad account dei clienti nel Business Manager. Poi `./run.sh`.

`config.yaml` e `clients_private.json` **non stanno nel repo** apposta: contengono
id degli ad account e nomi veri dei clienti.
