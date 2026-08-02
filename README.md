# Cardinal — veille autonome (version 100 % gratuite)

> Le système qui surveille et régule ton monde. Tu définis les sujets, il fait le reste.

Veille programmable, **tout dans le navigateur** : tu ajoutes un sujet, tu choisis
la fréquence, tu cliques « Lancer », tu lis le digest façon article. **Coût : 0 €.**

- **LLM** : API gratuite — **Gemini**, **Groq** ou **OpenRouter** (clé à coller dans le dashboard).
- **RSS** pour l'actu, **OSV** pour les CVE de dépendances, **NVD** pour les CVE de produits.
- Aucune de ces sources n'est payante.

## Démarrer (dashboard)

```bash
pip install -r requirements.txt
python app.py
```

Puis ouvre **http://127.0.0.1:5000**. Tout se passe là :

1. **Réglages** → choisis le fournisseur et **colle ta clé** (pas de terminal, pas de `.env`).
   Clé gratuite en 1 min, sans carte bancaire :
   - **Gemini** — https://aistudio.google.com/apikey (~1500 req/jour, conseillé)
   - **Groq** — https://console.groq.com/keys (très rapide)
2. **Tes sujets** s'affichent en cartes (déjà pré-remplis : AR, jeux vidéo, CVE produits…).
   Bouton **Lancer** → la recherche tourne, puis le digest s'ouvre dans une fenêtre.
3. **➕ Ajouter un sujet** → un formulaire ; choisis le type de source, l'aide s'adapte.

Les sujets sont stockés en base (`cardinal.db`) : plus besoin de toucher au code pour en ajouter.

## En ligne de commande (optionnel)

Le dashboard n'est pas obligatoire — tout marche aussi au terminal :

```bash
python agent.py list          # voir les sujets
python agent.py run gamedev   # -> digests/gamedev_<date>.html
```

Dans ce cas la clé peut venir d'un fichier `.env` (voir `.env.example`).

## Tester Cardinal

**1. Test hors-ligne (sans clé, sans réseau)** — vérifie toute la tuyauterie :

```bash
python selftest.py
```

Doit afficher `5/5 tests OK`. Ça valide le parsing RSS/NVD, l'extraction JSON,
le rendu HTML et la base SQLite avant de dépenser du quota.

**2. Test bout-en-bout (avec ta clé gratuite)** :

```bash
cp .env.example .env          # renseigne LLM_PROVIDER + 1 clé
python agent.py list          # doit lister les 5 sujets
python agent.py run gamedev   # RSS : flux gaming très actifs -> contenu quasi garanti
```

Ouvre le fichier `digests/gamedev_*.html` : tu dois voir un article avec des items
et des liens de sources **réels et cliquables**.

**Astuce pour forcer du contenu** : si un run renvoie « rien de neuf » (fenêtre vide),
élargis temporairement la fenêtre du sujet dans `topics.py`
(ex. `frequency_hours=720` = 30 jours), relance, puis remets la valeur normale.

**Tester chaque source une fois** :

```bash
python agent.py run gamedev    # source web  (RSS)
python agent.py run produits   # source nvd  (CVE produits — patiente ~6s/produit sans clé NVD)
python agent.py run mesvulns   # source osv  (CVE dépendances)
```

**Ce que tu vérifies** : le HTML s'ouvre, les items sont pertinents, les sources
pointent vers de vraies URLs, rien n'est inventé hors des données récupérées.

### En cas d'erreur

| Message | Cause / solution |
|---|---|
| `Clé manquante : renseigne GEMINI_API_KEY` | `.env` absent ou clé vide |
| Erreur `429` | quota/minute atteint — attends un peu, ou change de `LLM_PROVIDER` |
| Digest « rien de neuf » | fenêtre vide — élargis `frequency_hours` pour tester |
| Un flux RSS renvoie 0 item | l'URL du flux est morte/bloquée — teste-la dans le navigateur, ajuste `feeds` |
| NVD lent | normal sans clé (~6 s/produit) — une clé NVD gratuite accélère |



Trois types de source :

- **web** — actualité via **flux RSS** dans `feeds`. (Astuce : beaucoup de sites exposent
  un flux en ajoutant `/feed` ou `/rss` à l'URL.) Idéal aussi pour ce qui n'a pas de base
  CVE (ex. Freebox Ultra).
- **osv** — CVE de **dépendances de code** dans `packages` (`name` + `ecosystem` : PyPI,
  npm, Go, crates.io, Maven, NuGet…). Gratuit, sans clé.
- **nvd** — CVE de **produits / systèmes** dans `keywords` (iOS, Windows 11, WireGuard…).
  Recherche par mot-clé sur la base NVD.
  - Préfixe par le fabricant pour réduire le bruit : `"Apple iOS"` plutôt que `"iOS"`.
  - Pour VNC, précise l'implémentation (`RealVNC`, `TigerVNC`, `UltraVNC`…).
  - Option `min_severity="HIGH"` pour ne garder que le haut du panier.
  - Le LLM écarte les faux positifs (mot-clé qui apparaît par hasard).
  - Marche sans clé ; une clé NVD gratuite accélère (voir `.env.example`).

**Quel type pour quoi ?** Dépendance de projet → `osv`. Logiciel/OS/appliance → `nvd`.
Pas de CVE publiée (box, matériel grand public) → `web` (RSS).

## Changer de fournisseur / modèle

Tout se pilote par l'environnement, sans toucher au code :

```bash
LLM_PROVIDER=groq            # gemini | groq | openrouter
LLM_MODEL=llama-3.3-70b-versatile   # optionnel : forcer un modèle
```

## Fichiers

| Fichier | Rôle |
|---|---|
| `app.py` | **le dashboard web** (à lancer) |
| `topics.py` | le modèle `Topic` + les sujets par défaut (seed au 1er lancement) |
| `research.py` | l'agent : RSS+LLM (web), OSV (deps), NVD (produits), multi-fournisseurs |
| `render.py` | génère le HTML de l'article |
| `store.py` | SQLite : sujets, runs archivés, dédup, réglages/clés |
| `agent.py` | CLI (alternative au dashboard) |
| `selftest.py` | test hors-ligne de toute la mécanique |

## Limites de la version gratuite

- Les modèles gratuits (Gemini Flash, Llama 70B…) rédigent un cran en dessous d'un
  modèle frontière, mais restent très bons pour du résumé.
- Le web passe par RSS : tu listes tes sources (pas de recherche ouverte). En échange,
  c'est gratuit, rapide et sans hallucination hors des articles fournis.
- Quotas gratuits = plafonds par minute/jour. Sans impact pour une veille perso.

## Prochaines étapes

1. **Scheduling** : `python agent.py run <id>` en cron, ou APScheduler qui compare
   `frequency_hours` à la date du dernier run (table `runs`).
2. **Envoi par mail** en plus de l'écriture disque.
3. **Petite UI web** (FastAPI) pour gérer les sujets et lister les digests.
4. **Stacking de quotas** : router entre plusieurs fournisseurs gratuits en cas de 429.
