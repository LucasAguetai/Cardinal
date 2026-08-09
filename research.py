"""
Version 100 % gratuite.

Coût supprimé sur deux axes :
  - LLM   : API gratuite compatible OpenAI (Gemini, Groq ou OpenRouter),
            choisie via la variable d'env LLM_PROVIDER. Aucun code à changer.
  - Web   : flux RSS (gratuits, sans clé) au lieu de l'outil de recherche payant.
            Le LLM ne fait plus que trier/résumer les articles fournis.
  - CVE   : OSV.dev (déjà gratuit) + résumé par le même LLM.

Le digest renvoyé garde toujours la même forme (voir render.py).
"""

import os
import re
import json
import time
import calendar
import datetime as dt

import requests
import feedparser
from openai import (
    OpenAI, RateLimitError, AuthenticationError, BadRequestError, APIError,
)

import store  # pour lire les réglages (fournisseur, clés) saisis dans le dashboard


def _cfg(key: str, default=None):
    """Réglage : d'abord le dashboard (SQLite), sinon l'environnement (.env), sinon défaut."""
    try:
        v = store.get_setting(key)
    except Exception:
        v = None
    return v or os.getenv(key) or default

# --- Fournisseurs gratuits (endpoint compatible OpenAI + modèle par défaut) ---
PROVIDERS = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        # 2.5-flash : modèle gratuit actuel. (2.0-flash a été retiré côté Google →
        # 404 NotFound.) Surchargeable via LLM_MODEL si Google le retire à son tour.
        "model": "gemini-2.5-flash",
        "key_env": "GEMINI_API_KEY",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "key_env": "GROQ_API_KEY",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        # ⚠️ Les modèles ":free" d'OpenRouter changent souvent. Si tu as un 404
        # "unavailable for free", change ce modèle via LLM_MODEL (voir la liste sur
        # openrouter.ai/models?max_price=0).
        "model": "inclusionai/ling-3.0-flash:free",
        "key_env": "OPENROUTER_API_KEY",
    },
}


# --------------------------------------------------------------------------
# Client LLM (avec basculement automatique de fournisseur)
# --------------------------------------------------------------------------
# Ordre de préférence quand plusieurs fournisseurs ont une clé.
PROVIDER_ORDER = ("gemini", "groq", "openrouter")


def _provider_key(prov: str):
    """Clé d'un fournisseur : sa clé dédiée (réglages/​.env), ou la clé générique
    LLM_API_KEY s'il s'agit du fournisseur sélectionné (compat ancienne config)."""
    key = _cfg(PROVIDERS[prov]["key_env"])
    if key:
        return key
    if prov == (_cfg("LLM_PROVIDER", "gemini")).lower():
        return _cfg("LLM_API_KEY")
    return None


def _provider_chain() -> list:
    """Fournisseurs à essayer, dans l'ordre : celui choisi d'abord, puis les
    autres qui ont une clé. Permet de basculer tout seul si l'un est à court
    de quota."""
    configured = (_cfg("LLM_PROVIDER", "gemini")).lower()
    order = [configured] + [p for p in PROVIDER_ORDER if p != configured]
    chain = []
    for p in order:
        if p in PROVIDERS and p not in chain and _provider_key(p):
            chain.append(p)
    return chain


def _client_for(prov: str):
    cfg = PROVIDERS[prov]
    # timeout borne chaque appel ; max_retries=0 = on échoue VITE sur un 429 (sinon
    # le SDK réessaie tout seul avec backoff et le basculement traîne).
    client = OpenAI(base_url=cfg["base_url"], api_key=_provider_key(prov),
                    timeout=30.0, max_retries=0)
    # Modèle : défaut du fournisseur, surchargeable par fournisseur via
    # {PROVIDER}_MODEL (utile car les modèles gratuits d'OpenRouter changent
    # souvent), puis par LLM_MODEL pour le fournisseur choisi.
    model = _cfg(f"{prov.upper()}_MODEL", cfg["model"])
    if prov == (_cfg("LLM_PROVIDER", "gemini")).lower():
        model = _cfg("LLM_MODEL", model)
    return client, model


class _ProviderExhausted(Exception):
    """Ce fournisseur ne peut pas servir la requête (quota, indispo, clé KO) :
    on tente le suivant."""


def _extract_json(text: str) -> dict:
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Aucun JSON trouvé dans la réponse du modèle.")
    return json.loads(text[start : end + 1])


# Plafond de sources envoyées au LLM en un seul appel : garde le prompt sous les
# limites "tokens par minute" des paliers gratuits (Groq : 12 000/min).
MAX_LLM_ITEMS = 20

# Les CVE sont volumineuses (description + CVSS + références). Envoyer 20 CVE d'un
# coup fait exploser la limite "tokens par requête" de certains paliers gratuits
# (Groq -> 413). On découpe donc en petits lots, chacun sous la limite, puis on
# fusionne. Résultat : un gros sujet passe sur Groq comme les petits.
CVE_BATCH = 6

# Pause entre deux lots de CVE : lisse les rafales d'appels LLM pour rester sous
# la limite « requêtes par minute » des paliers gratuits (Gemini -> 429).
CVE_BATCH_PAUSE_S = float(_cfg("CVE_BATCH_PAUSE", "2"))


def _is_too_large(err) -> bool:
    """Vrai si l'erreur est un 413 / « requête trop grosse » (dépassement du
    budget de tokens par minute), à distinguer d'un simple 429."""
    if getattr(err, "status_code", None) == 413:
        return True
    s = str(err).lower()
    return "too large" in s or "reduce your message" in s


def _quota_hint(err) -> str:
    """Décode un 429 pour dire QUELLE limite a sauté (par minute / par jour)
    et dans combien de temps réessayer. Renvoie "" si l'info est absente."""
    payload = getattr(err, "body", None)
    if not isinstance(payload, dict):
        try:
            payload = err.response.json()
        except Exception:
            payload = None
    details = []
    if isinstance(payload, dict):
        details = (payload.get("error", {}) or {}).get("details", []) or []

    quota_id, retry = None, None
    for d in details:
        t = d.get("@type", "")
        if "QuotaFailure" in t:
            viols = d.get("violations", [])
            if viols:
                quota_id = viols[0].get("quotaId") or viols[0].get("quotaMetric")
        elif "RetryInfo" in t:
            retry = d.get("retryDelay")

    parts = []
    if quota_id and "PerMinute" in quota_id:
        parts.append("limite PAR MINUTE (se lève en ~1 min)")
    elif quota_id and "PerDay" in quota_id:
        parts.append("limite PAR JOUR (réinitialisée le lendemain, minuit Pacifique)")
    elif quota_id:
        parts.append(f"quota : {quota_id}")
    if retry:
        parts.append(f"réessai conseillé dans ~{retry}")
    return " · ".join(parts)


def _call_provider(client, model, messages, extra=None) -> dict:
    """Un appel à UN fournisseur. Renvoie le JSON, ou lève _ProviderExhausted si
    ce fournisseur ne peut pas répondre (quota, indispo, clé KO, réponse vide) —
    au caller de tenter le suivant."""
    # max_tokens = tokens de sortie RÉSERVÉS. Attention : certains fournisseurs
    # (Groq) les comptent dans leur budget "tokens par minute" -> trop haut = 413
    # même avec une seule requête. 4096 suffit pour un digest.
    base = dict(model=model, temperature=0.3, max_tokens=4096, messages=messages)
    a1 = {**base, "response_format": {"type": "json_object"}}
    if extra:                       # ex. OpenRouter : couper le "raisonnement"
        a1["extra_body"] = extra    # (sinon le modèle brûle tous ses tokens et
    a2 = {**base}                   #  renvoie du vide) — voir _llm_json.
    if extra:
        a2["extra_body"] = extra
    attempts = [a1, a2]  # a2 = repli si response_format non supporté

    last = ""
    for kw in attempts:
        try:
            resp = client.chat.completions.create(**kw)
        except BadRequestError as e:
            last = f"option refusée : {e}"
            continue  # ex. response_format non supporté -> on tente sans
        except RateLimitError as e:
            hint = _quota_hint(e)
            raise _ProviderExhausted(f"quota atteint (429){' · ' + hint if hint else ''}")
        except AuthenticationError:
            raise _ProviderExhausted("clé refusée (401)")
        except APIError as e:
            if _is_too_large(e):
                raise _ProviderExhausted("requête trop volumineuse (413)")
            if getattr(e, "status_code", None) == 404 or "unavailable for free" in str(e).lower():
                raise _ProviderExhausted(
                    "modèle indisponible (404) — PAS un souci de clé. Change le "
                    "modèle du fournisseur dans les Réglages (les modèles gratuits "
                    "OpenRouter changent souvent)."
                )
            raise _ProviderExhausted(f"erreur API : {e}")

        choice = resp.choices[0]
        content = (choice.message.content or "").strip()
        if not content:
            last = f"réponse vide (finish_reason={getattr(choice, 'finish_reason', '?')})"
            continue
        try:
            return _extract_json(content)
        except Exception:
            last = "pas de JSON exploitable dans la réponse"

    raise _ProviderExhausted(last or "échec inconnu")


def _llm_json(system: str, user: str) -> dict:
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    chain = _provider_chain()
    if not chain:
        raise RuntimeError(
            "Clé LLM manquante : renseigne au moins une clé dans les Réglages du "
            "dashboard (ou dans .env)."
        )

    errors = []
    for prov in chain:
        client, model = _client_for(prov)
        # OpenRouter n'a plus que des modèles gratuits "à raisonnement" qui
        # renvoient du vide -> on désactive le raisonnement pour ce fournisseur.
        extra = {"reasoning": {"enabled": False}} if prov == "openrouter" else None
        try:
            return _call_provider(client, model, messages, extra)
        except _ProviderExhausted as e:
            errors.append(f"{prov} → {e}")
            continue  # basculement automatique vers le fournisseur suivant

    detail = " ; ".join(errors)
    if len(chain) > 1:
        raise RuntimeError(
            f"Tous les fournisseurs LLM configurés ont échoué ({detail}). "
            "Attends la réinitialisation d'un quota, ou ajoute une clé de repli."
        )
    raise RuntimeError(
        f"Le fournisseur LLM a échoué ({detail}). Ajoute une clé de repli (ex. "
        "Groq) dans les Réglages pour un basculement automatique, ou attends la "
        "réinitialisation du quota."
    )


def _since(frequency_hours: int) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=frequency_hours)


# Fenêtre de recherche CVE : au moins ~3 mois. Les vulnérabilités d'un produit
# sont rarement publiées "cette semaine" ; regarder trop court = feed vide. Le
# feed (dédup par URL + fenêtre glissante de 30 j) évite l'accumulation. NVD
# limite une requête à 120 jours -> on reste sous ce plafond.
CVE_LOOKBACK_DAYS = 90


def _cve_since(topic) -> dt.datetime:
    hours = max(topic.frequency_hours, CVE_LOOKBACK_DAYS * 24)
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)


# --------------------------------------------------------------------------
# Backend "web" : flux RSS -> LLM
# --------------------------------------------------------------------------
_TAGS = re.compile(r"<[^>]+>")


def _clean(txt: str) -> str:
    return re.sub(r"\s+", " ", _TAGS.sub(" ", txt or "")).strip()


_UA = "Mozilla/5.0 (compatible; Cardinal/1.0; +veille)"


def _fetch_feed_items(feeds: list, since: dt.datetime, max_items: int = 25) -> list:
    items = []
    for url in feeds:
        try:
            d = feedparser.parse(url, agent=_UA)  # UA navigateur : certains flux bloquent les bots
        except Exception as e:
            print(f"  ! RSS échec {url}: {e}")
            continue
        source = d.feed.get("title", url) if getattr(d, "feed", None) else url
        for e in d.entries:
            ts = None
            if getattr(e, "published_parsed", None):
                ts = dt.datetime.fromtimestamp(calendar.timegm(e.published_parsed), dt.timezone.utc)
            if ts and ts < since:
                continue  # trop vieux pour la fenêtre
            items.append(
                {
                    "title": _clean(getattr(e, "title", "")),
                    "summary": _clean(getattr(e, "summary", ""))[:400],
                    "link": getattr(e, "link", ""),
                    "published": ts.isoformat() if ts else "",
                    "source": source,
                }
            )
    items.sort(key=lambda x: x["published"], reverse=True)
    return items[:max_items]


SYSTEM_WEB = """Tu es un rédacteur de veille. On te fournit des articles récents
(titre, résumé, lien) issus de flux RSS. Tu écris un digest en français.

Règles STRICTES :
- Ne te sers QUE des articles fournis. N'invente aucune information ni aucun lien.
- Sélectionne et regroupe les 3 à 6 sujets les plus importants (ignore le bruit).
- Chaque item : un titre, 1 à 3 paragraphes de synthèse, et pour "sources" les liens
  RÉELS des articles utilisés (champ link).
- importance = high / medium / low selon l'intérêt.

Réponds UNIQUEMENT par un objet JSON :
{"headline": "...", "summary": "...", "items": [
  {"title": "...", "body": "...", "importance": "high|medium|low",
   "sources": [{"title": "...", "url": "..."}]}]}"""


# Actu/RSS : on limite le nombre d'articles ET on raccourcit chaque entrée. Les
# liens Google News (redirections) sont très longs -> sinon le prompt dépasse la
# limite "tokens par minute" des paliers gratuits (Groq : 12 000/min = 413).
MAX_ARTICLES = 25


def _summarize_articles(topic, feeds: list, since) -> dict:
    """Récupère les articles de `feeds` depuis `since`, puis les fait résumer.
    Partagé par le backend RSS (flux fournis) et le backend "news" (recherche)."""
    items = _fetch_feed_items(feeds, since)
    items = items[:MAX_ARTICLES]

    # NB : la dédup se fait au niveau du feed (par URL), pas ici — on renvoie donc
    # tous les articles de la fenêtre, et le feed ignore ceux déjà présents.
    if not items:
        return {
            "headline": f"{topic.name} — rien de neuf",
            "summary": "Aucun article récent trouvé sur la fenêtre choisie.",
            "items": [],
        }

    # Payload COMPACT pour le LLM (titre + résumé court + lien + source).
    compact = [
        {"title": it["title"], "summary": (it["summary"] or "")[:200],
         "link": it["link"], "source": it["source"]}
        for it in items
    ]
    user = (
        f"Sujet : {topic.name}\n"
        f"Fenêtre : depuis {since.strftime('%Y-%m-%d')}.\n"
        f"Articles ({len(compact)}) :\n"
        f"{json.dumps(compact, ensure_ascii=False)}"
    )
    digest = _llm_json(SYSTEM_WEB, user)
    digest["_item_keys"] = [it["link"] for it in items if it["link"]]  # dédup future
    return digest


def research_web(topic) -> dict:
    if not topic.feeds:
        raise RuntimeError(f"Le sujet '{topic.id}' n'a aucun flux RSS (champ feeds).")
    return _summarize_articles(topic, topic.feeds, _since(topic.frequency_hours))


# --------------------------------------------------------------------------
# Backend "news" : recherche d'actu via Google News RSS (gratuit, sans clé).
# L'utilisateur ne fournit AUCUN lien : il tape juste des sujets, on construit
# la requête. Réutilise ensuite exactement le pipeline RSS ci-dessus.
# --------------------------------------------------------------------------
def _gnews_url(query: str) -> str:
    q = requests.utils.quote(query.strip())
    return f"https://news.google.com/rss/search?q={q}&hl=fr&gl=FR&ceid=FR:fr"


# L'actu d'un sujet n'est pas forcément publiée "aujourd'hui" (un film annoncé
# il y a quelques semaines compte encore). On regarde ~30 jours en arrière (=
# la rétention du feed), indépendamment de la fréquence de vérification. Le feed
# (dédup + purge 30 j) évite l'accumulation.
NEWS_LOOKBACK_DAYS = 30


def _news_since(topic) -> dt.datetime:
    hours = max(topic.frequency_hours, NEWS_LOOKBACK_DAYS * 24)
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)


def research_news(topic) -> dict:
    queries = [q for q in topic.keywords if q.strip()]
    if not queries:
        raise RuntimeError(f"Le sujet '{topic.id}' n'a aucun sujet à suivre.")
    feeds = [_gnews_url(q) for q in queries]
    return _summarize_articles(topic, feeds, _news_since(topic))


# --------------------------------------------------------------------------
# Backend "osv" (CVE) : OSV.dev gratuit -> LLM
# --------------------------------------------------------------------------
OSV_QUERY_URL = "https://api.osv.dev/v1/query"


def _osv_fixed_versions(vuln: dict, package_name: str) -> list:
    """Versions corrigées d'une vulnérabilité OSV pour un paquet donné : on lit
    les évènements 'fixed' des plages de versions affectées."""
    fixed = []
    for aff in vuln.get("affected", []):
        if aff.get("package", {}).get("name") not in (None, package_name):
            continue
        for rng in aff.get("ranges", []):
            for ev in rng.get("events", []):
                if ev.get("fixed"):
                    fixed.append(ev["fixed"])
    # ordre stable, sans doublon
    return sorted(set(fixed))


def _fetch_osv(package: dict, since: dt.datetime) -> list:
    body = {"package": {"name": package["name"], "ecosystem": package["ecosystem"]}}
    r = requests.post(OSV_QUERY_URL, json=body, timeout=30)
    r.raise_for_status()
    out = []
    for v in r.json().get("vulns", []):
        published = v.get("published") or v.get("modified")
        if not published:
            continue
        ts = dt.datetime.fromisoformat(published.replace("Z", "+00:00"))
        if ts >= since:
            out.append(
                {
                    "id": v.get("id"),
                    "package": package["name"],
                    "ecosystem": package["ecosystem"],
                    "summary": v.get("summary", ""),
                    "details": (v.get("details", "") or "")[:800],
                    "published": published,
                    "severity": v.get("severity", []),
                    "fixed": _osv_fixed_versions(v, package["name"]),
                    "references": [ref.get("url") for ref in v.get("references", [])][:3],
                }
            )
    return out


SYSTEM_OSV = """Tu es un analyste sécurité. On te donne des vulnérabilités OSV.dev
pour les paquets d'un utilisateur. Rédige un digest en français, priorisé.

- Un item par vulnérabilité notable (regroupe les doublons).
- Commence TOUJOURS le titre par le paquet affecté entre crochets (champ "package"),
  ex. "[Next.js] ...", "[requests] déni de service via...". Puis décris le risque.
- Explique : quel paquet, quel risque.
- importance = high pour critique/haute, medium pour moyenne, low sinon.
- sources : réutilise les urls de références fournies.
- "fix" = comment se mettre à jour, en clair et court, à partir du champ "fixed" :
  * une seule version -> "≥ 2.32.0".
  * plusieurs versions (branches différentes) -> liste-les, séparées par des virgules,
    SANS répéter «≥», et précise le choix. Ex. "corrigé en 10.6.26, 10.11.17, 11.4.11
    ou 11.8.7 — prends la version de ta branche".
  * "fixed" vide/absent -> "correctif non précisé — voir les références".

Réponds UNIQUEMENT par un objet JSON :
{"headline": "...", "summary": "...", "items": [
  {"title": "...", "body": "...", "importance": "high|medium|low",
   "fix": "≥ 2.32.0", "sources": [{"title": "...", "url": "..."}]}]}"""


def _summarize_cve_batches(system: str, since, vulns: list, item_keys: list) -> dict:
    """Résume des CVE en les DÉCOUPANT en petits lots (CVE_BATCH) pour rester sous
    la limite tokens/requête des paliers gratuits, puis fusionne les items. Le JSON
    est envoyé COMPACT (sans indentation) : moins de tokens à traiter."""
    since_str = since.strftime("%Y-%m-%d")
    items, headline, summary = [], "", ""
    for i in range(0, len(vulns), CVE_BATCH):
        if i and CVE_BATCH_PAUSE_S > 0:
            time.sleep(CVE_BATCH_PAUSE_S)  # lisse les rafales -> moins de 429
        batch = vulns[i:i + CVE_BATCH]
        user = (
            f"Fenêtre : depuis {since_str}.\n"
            f"CVE ({len(batch)}) :\n{json.dumps(batch, ensure_ascii=False)}"
        )
        part = _llm_json(system, user)
        items.extend(part.get("items", []))
        if not headline:  # on garde le titre/résumé du premier lot analysé
            headline, summary = part.get("headline", ""), part.get("summary", "")
    return {"headline": headline, "summary": summary, "items": items, "_item_keys": item_keys}


def research_osv(topic) -> dict:
    since = _cve_since(topic)
    vulns = []
    for pkg in topic.packages:
        try:
            vulns.extend(_fetch_osv(pkg, since))
        except Exception as e:
            print(f"  ! OSV échec {pkg['name']} ({pkg['ecosystem']}): {e}")

    # Dédup gérée au niveau du feed (par URL) : on envoie toutes les CVE de la
    # fenêtre au LLM, le feed ignorera celles déjà présentes.
    vulns = vulns[:MAX_LLM_ITEMS]  # garde le prompt sous la limite tokens/minute

    if not vulns:
        return {
            "headline": f"{topic.name} — aucune CVE",
            "summary": f"Aucune vulnérabilité connue sur les {CVE_LOOKBACK_DAYS} derniers "
                       "jours pour tes paquets. RAS.",
            "items": [],
        }

    return _summarize_cve_batches(SYSTEM_OSV, since, vulns, [v["id"] for v in vulns])


# --------------------------------------------------------------------------
# Backend "nvd" (CVE par produit) : NVD gratuit -> LLM
# --------------------------------------------------------------------------
# NVD = base de référence, cherchable PAR PRODUIT (iOS, Windows 11, WireGuard...).
# Recherche par mot-clé (keywordSearch) : simple mais un peu bruitée -> le LLM filtre.
# Fenêtre de dates obligatoire par paire, max 120 jours (largement suffisant ici).
NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"


def _nvd_headers():
    key = _cfg("NVD_API_KEY")  # optionnel : https://nvd.nist.gov/developers/request-an-api-key
    return {"apiKey": key} if key else {}


def _nvd_delay() -> float:
    # Sans clé : 5 req / 30 s -> ~6 s. Avec clé : 50 / 30 s -> ~1 s.
    return 1.0 if _cfg("NVD_API_KEY") else 6.0


def _nvd_fmt(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S.000")


def _fetch_nvd(keyword: str, since: dt.datetime, now: dt.datetime, min_severity: str) -> list:
    params = {
        "keywordSearch": keyword,
        "pubStartDate": _nvd_fmt(since),
        "pubEndDate": _nvd_fmt(now),
        "resultsPerPage": 50,
    }
    if min_severity:
        params["cvssV3Severity"] = min_severity.upper()  # LOW|MEDIUM|HIGH|CRITICAL

    r = requests.get(NVD_URL, params=params, headers=_nvd_headers(), timeout=40)
    r.raise_for_status()

    out = []
    for v in r.json().get("vulnerabilities", []):
        cve = v.get("cve", {})
        descs = cve.get("descriptions", [])
        desc = next((d["value"] for d in descs if d.get("lang") == "en"), "")
        # Sévérité : on prend la meilleure métrique disponible (v3.1 > v3.0 > v2)
        metrics = cve.get("metrics", {})
        sev = ""
        for k in ("cvssMetricV31", "cvssMetricV30"):
            if metrics.get(k):
                sev = metrics[k][0]["cvssData"].get("baseSeverity", "")
                break
        if not sev and metrics.get("cvssMetricV2"):
            sev = metrics["cvssMetricV2"][0].get("baseSeverity", "")
        out.append(
            {
                "id": cve.get("id"),
                "matched": keyword,
                "summary": desc[:600],
                "published": cve.get("published", ""),
                "severity": sev,
                "fixed": _nvd_fixed_versions(cve),
                "references": [ref.get("url") for ref in cve.get("references", [])][:3],
            }
        )
    return out


def _nvd_fixed_versions(cve: dict) -> list:
    """Version(s) corrigée(s) d'une CVE NVD : `versionEndExcluding` d'une plage
    CPE = la version À PARTIR DE LAQUELLE la faille est corrigée. Best-effort
    (NVD ne donne pas toujours cette info)."""
    fixed = []
    for conf in cve.get("configurations", []):
        for node in conf.get("nodes", []):
            for m in node.get("cpeMatch", []):
                v = m.get("versionEndExcluding")
                if v:
                    fixed.append(v)
    return sorted(set(fixed))


SYSTEM_NVD = """Tu es un analyste sécurité. On te donne des CVE trouvées sur NVD par
recherche PAR MOT-CLÉ, pour les produits qu'un utilisateur déclare utiliser. Cette
recherche est TRÈS BRUYANTE : le mot-clé peut apparaître par hasard dans une CVE qui
ne concerne pas du tout le produit. Ton rôle principal est de FILTRER strictement.

Le champ `matched` = le mot-clé (donc le produit) recherché. Règles de pertinence :
- Ne garde une CVE QUE si le produit RÉELLEMENT vulnérable est bien ce produit précis
  (ou un composant officiel de ce produit).
- Si `matched` est un LANGAGE ou un runtime (Python, Java, Node, PHP, Go…), ne garde
  QUE les CVE de l'interpréteur / runtime OFFICIEL lui-même. ÉCARTE toute CVE d'une
  bibliothèque ou d'un logiciel TIERS écrit dans ce langage : une faille d'un paquet
  PyPI/npm n'est PAS une faille de « Python » ou « Node ».
- Écarte de même les CVE d'outils/applications tierces qui ne font que mentionner le
  produit (ex. un plugin qui tourne "sur Ubuntu" n'est pas une faille d'Ubuntu).
- IMPORTANT : si le produit VULNÉRABLE n'est pas le produit recherché lui-même
  (ex. "Windows 11" recherché, mais la faille est dans MariaDB installé dessus),
  ÉCARTE : ce n'est PAS une faille du produit de l'utilisateur.
- Dans le DOUTE, ÉCARTE. Mieux vaut 0 item sûr que du bruit.
- Si après filtrage il ne reste rien de pertinent, renvoie "items": [] (liste vide).

Pour chaque item conservé :
- Commence le titre par le produit RÉELLEMENT vulnérable entre crochets (le vrai
  logiciel touché, qui doit être le produit recherché) : "[Windows 11] ...".
- Explique le risque.
- importance = high pour CRITICAL/HIGH, medium pour MEDIUM, low sinon.
- sources : réutilise les urls de références fournies.
- "fix" = comment se mettre à jour, en clair et court, à partir du champ "fixed" :
  * une seule version -> "≥ 17.2".
  * plusieurs versions (branches) -> liste-les séparées par des virgules SANS répéter
    «≥», ex. "corrigé en 10.6.26, 10.11.17 ou 11.4.11 — prends celle de ta branche".
  * "fixed" vide/absent -> "correctif non précisé — voir les références".

Réponds UNIQUEMENT par un objet JSON :
{"headline": "...", "summary": "...", "items": [
  {"title": "...", "body": "...", "importance": "high|medium|low",
   "fix": "≥ 17.2", "sources": [{"title": "...", "url": "..."}]}]}"""


def research_nvd(topic) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    since = _cve_since(topic)

    by_id = {}
    for i, kw in enumerate(topic.keywords):
        if i:
            time.sleep(_nvd_delay())  # respect du rate limit entre requêtes
        try:
            for cve in _fetch_nvd(kw, since, now, topic.min_severity):
                by_id.setdefault(cve["id"], cve)  # dédup : une CVE peut matcher plusieurs mots-clés
        except Exception as e:
            print(f"  ! NVD échec pour '{kw}': {e}")

    # Dédup gérée au niveau du feed (par URL) : on envoie toutes les CVE de la
    # fenêtre au LLM, le feed ignorera celles déjà présentes.
    vulns = list(by_id.values())[:MAX_LLM_ITEMS]  # sous la limite tokens/minute

    if not vulns:
        return {
            "headline": f"{topic.name} — aucune CVE",
            "summary": f"Aucune CVE trouvée sur les {CVE_LOOKBACK_DAYS} derniers jours "
                       "pour tes produits. RAS.",
            "items": [],
        }

    return _summarize_cve_batches(SYSTEM_NVD, since, vulns, list(by_id.keys()))


# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Backend "kev" (CVE activement exploitées) : CISA KEV -> items directs (sans LLM)
# --------------------------------------------------------------------------
# La liste KEV de la CISA recense les CVE EXPLOITÉES dans la nature, mise à jour
# en quasi temps réel. Données déjà propres et curées -> on construit les items
# SANS LLM (donc AUCUN risque de quota 429/413/404). On matche les mots-clés
# produits de l'utilisateur sur le couple éditeur+produit (champs structurés),
# ce qui limite le bruit. KEV suit le produit AMONT (ex. « Windows », « Linux
# Kernel », « OpenSSH ») : tape le nom amont, pas la distro (Ubuntu/CentOS…).
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
KEV_MAX_ITEMS = 30


def research_kev(topic) -> dict:
    since = _cve_since(topic)
    # Limites de mots : « iOS » matche « Apple iOS » mais PAS « FortiOS »/« BIOS »
    # (sous-mot). Astuce : préfixe l'éditeur (« Apple iOS ») pour ne pas attraper
    # « Cisco IOS ». Match sur éditeur+produit (structuré), pas la description.
    pats = [re.compile(r"\b" + re.escape(k.strip()) + r"\b", re.I)
            for k in topic.keywords if k.strip()]

    r = requests.get(KEV_URL, timeout=40)
    r.raise_for_status()
    catalog = r.json().get("vulnerabilities", [])

    matched = []
    for v in catalog:
        try:
            added = dt.datetime.fromisoformat(v.get("dateAdded", "")).replace(tzinfo=dt.timezone.utc)
        except Exception:
            continue
        if added < since:
            continue
        hay = f"{v.get('vendorProject', '')} {v.get('product', '')}"
        if pats and not any(p.search(hay) for p in pats):
            continue
        matched.append((added, v))

    matched.sort(key=lambda x: x[0], reverse=True)  # plus récentes d'abord
    matched = matched[:KEV_MAX_ITEMS]

    if not matched:
        return {
            "headline": f"{topic.name} — aucune CVE exploitée",
            "summary": f"Aucune CVE activement exploitée (CISA KEV) ajoutée sur les "
                       f"{CVE_LOOKBACK_DAYS} derniers jours pour tes produits. RAS.",
            "items": [], "_item_keys": [],
        }

    items, item_keys = [], []
    for added, v in matched:
        cve = v.get("cveID", "")
        product = f"{v.get('vendorProject', '')} {v.get('product', '')}".strip()
        due = v.get("dueDate", "")
        body = (v.get("shortDescription", "") or "").strip()
        action = (v.get("requiredAction", "") or "").strip()
        if action:
            body += f"\n\nAction CISA : {action}"
        if v.get("knownRansomwareCampaignUse", "") == "Known":
            body += "\n\n⚠️ Utilisée dans des campagnes de rançongiciel connues."
        items.append({
            "title": f"[{product}] {cve} — {v.get('vulnerabilityName', '') or 'exploitée'}",
            "body": body,
            "importance": "high",  # exploitée dans la nature -> priorité maximale
            "fix": f"Correctif éditeur — échéance CISA {due}" if due else "Appliquer le correctif éditeur",
            "sources": [{"title": cve, "url": f"https://nvd.nist.gov/vuln/detail/{cve}"}],
        })
        item_keys.append(cve)

    return {
        "headline": f"{topic.name} — {len(items)} CVE exploitée(s)",
        "summary": "CVE activement exploitées (source CISA KEV) touchant tes produits — "
                   "à corriger en priorité.",
        "items": items, "_item_keys": item_keys,
    }


def research(topic) -> dict:
    if topic.source == "news":
        return research_news(topic)
    if topic.source == "web":
        return research_web(topic)
    if topic.source == "osv":
        return research_osv(topic)
    if topic.source == "nvd":
        return research_nvd(topic)
    if topic.source == "kev":
        return research_kev(topic)
    raise ValueError(f"Source inconnue : {topic.source}")
