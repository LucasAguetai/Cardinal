"""
Test hors-ligne de Cardinal : vérifie toute la tuyauterie SANS clé API ni réseau.
À lancer avant un vrai run pour s'assurer que tout est en place.

    python selftest.py

Ce qui est testé : chargement des sujets, parsing RSS, parsing NVD, extraction
JSON du LLM, rendu HTML, base SQLite. L'appel LLM réel n'est PAS testé ici
(il demande une clé) — voir le README pour le test bout-en-bout.
"""

import os
import datetime as dt

from topics import DEFAULT_TOPICS
import research
import render
import store

# Sécurité : le self-test tourne sur une base JETABLE, jamais sur cardinal.db (live).
store.DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selftest.db")
os.environ["CARDINAL_DB"] = store.DB_PATH   # pour que `import app` vise la même base
if os.path.exists(store.DB_PATH):
    os.remove(store.DB_PATH)

PASS, FAIL = "\033[92mOK\033[0m", "\033[91mÉCHEC\033[0m"
results = []


def check(name, fn):
    try:
        fn()
        results.append((name, True, ""))
        print(f"  [{PASS}] {name}")
    except Exception as e:
        results.append((name, False, str(e)))
        print(f"  [{FAIL}] {name} — {e}")


# 1) Les sujets se chargent
def t_topics():
    assert DEFAULT_TOPICS, "aucun sujet"
    for t in DEFAULT_TOPICS.values():
        assert t.source in ("news", "web", "osv", "nvd"), f"source invalide: {t.source}"


# 2) Parsing RSS (flux factice local, pas de réseau)
def t_rss():
    xml = """<?xml version="1.0"?><rss version="2.0"><channel><title>Demo</title>
    <item><title>Article récent</title><description>Un &lt;b&gt;test&lt;/b&gt;.</description>
    <link>https://example.com/a</link><pubDate>Mon, 28 Jul 2026 08:00:00 GMT</pubDate></item>
    <item><title>Trop vieux</title><description>hors fenêtre</description>
    <link>https://example.com/old</link><pubDate>Mon, 01 Jan 2024 08:00:00 GMT</pubDate></item>
    </channel></rss>"""
    with open("_selftest_feed.xml", "w") as f:
        f.write(xml)
    since = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)
    items = research._fetch_feed_items(["_selftest_feed.xml"], since)
    os.remove("_selftest_feed.xml")
    assert len(items) == 1, f"attendu 1 item dans la fenêtre, obtenu {len(items)}"
    assert items[0]["title"] == "Article récent"
    assert "<b>" not in items[0]["summary"], "HTML non nettoyé"


# 3) Parsing NVD (réponse API factice, pas de réseau)
def t_nvd():
    fake = {"vulnerabilities": [
        {"cve": {"id": "CVE-2026-0001", "published": "2026-07-27T10:00:00.000",
                 "descriptions": [{"lang": "fr", "value": "fr"}, {"lang": "en", "value": "A WireGuard flaw."}],
                 "metrics": {"cvssMetricV31": [{"cvssData": {"baseSeverity": "HIGH"}}]},
                 "references": [{"url": "https://nvd.example/1"}]}},
    ]}

    class R:
        def raise_for_status(self): pass
        def json(self): return fake

    orig = research.requests.get
    research.requests.get = lambda *a, **k: R()
    try:
        now = dt.datetime.now(dt.timezone.utc)
        out = research._fetch_nvd("WireGuard", now - dt.timedelta(hours=72), now, "")
    finally:
        research.requests.get = orig
    assert len(out) == 1
    assert out[0]["severity"] == "HIGH"
    assert out[0]["summary"].startswith("A WireGuard"), "mauvaise langue prise"


# 4) Extraction JSON de la réponse LLM (tolérante)
def t_json():
    a = research._extract_json('bla ```json\n{"headline":"h","summary":"s","items":[]}\n``` fin')
    assert a["headline"] == "h"
    b = research._extract_json('texte {"a":1,"b":{"c":2}} suite')
    assert b["b"]["c"] == 2


# 5) Rendu HTML + base SQLite
def t_render_db():
    store.init_db()
    t = next(iter(DEFAULT_TOPICS.values()))
    digest = {"headline": "Test", "summary": "Résumé.", "items": [
        {"title": "Item A", "body": "para 1\npara 2", "importance": "high",
         "sources": [{"title": "src", "url": "https://e.com"}]}]}
    html = render.render_html(digest, t)
    assert "<html" in html and "Item A" in html and len(html) > 500
    store.save_run(t.id, "digests/_selftest.html", digest)


# 6) Feed continu : accumulation, dédup, fenêtre glissante, rendu
def t_feed():
    store.init_db()
    t = next(iter(DEFAULT_TOPICS.values()))
    with store._conn() as c:                       # test isolé de tout état résiduel
        c.execute("DELETE FROM feed_items WHERE topic_id=?", (t.id,))
    items = [{"title": "Item A", "body": "b", "importance": "high", "fix": "≥ 2.32.0",
              "sources": [{"title": "s", "url": "https://e.com"}]}]
    n1 = len(store.add_feed_items(t.id, items))
    n2 = len(store.add_feed_items(t.id, items))     # même contenu -> ignoré
    assert n1 == 1 and n2 == 0, f"dédup KO ({n1},{n2})"

    feed = store.get_feed(t.id)
    assert len(feed) == 1 and feed[0]["title"] == "Item A"
    assert feed[0]["sources"][0]["url"] == "https://e.com", "sources non désérialisées"
    assert feed[0]["fix"] == "≥ 2.32.0", "version corrigée non persistée"
    assert "2.32.0" in render.render_feed(t, feed), "fix non affiché dans le feed"

    # Dédup par URL : le même lien reformulé (titre différent) NE crée PAS de doublon.
    reworded = [{"title": "Item A — autre formulation", "body": "b2", "importance": "low",
                 "sources": [{"title": "s", "url": "https://e.com"}]}]
    assert len(store.add_feed_items(t.id, reworded)) == 0, "dédup par URL KO"
    assert len(store.get_feed(t.id)) == 1

    # Un item vieux de 40 j doit disparaître à la purge ; le récent reste.
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40)).isoformat()
    with store._conn() as c:
        c.execute("INSERT INTO feed_items "
                  "(topic_id,item_key,title,body,importance,sources,created_at) "
                  "VALUES (?,?,?,?,?,?,?)",
                  (t.id, "vieux", "Vieux", "b", "low", "[]", old))
    store.purge_feed(days=30)
    titles = [f["title"] for f in store.get_feed(t.id, days=30)]
    assert "Vieux" not in titles and "Item A" in titles, f"purge KO: {titles}"

    html = render.render_feed(t, store.get_feed(t.id))
    assert "<html" in html and "Item A" in html and len(html) > 500


# Outils de simulation d'un client LLM (réponses scriptées / exceptions).
class _Msg:
    def __init__(self, c): self.content = c
class _Choice:
    def __init__(self, c): self.message = _Msg(c); self.finish_reason = "length"
class _Resp:
    def __init__(self, c): self.choices = [_Choice(c)]
class _Completions:
    def __init__(self, seq): self.seq = list(seq); self.i = 0
    def create(self, **kw):
        r = self.seq[min(self.i, len(self.seq) - 1)]; self.i += 1
        if isinstance(r, Exception):
            raise r
        return _Resp(r)
class _Client:
    def __init__(self, seq): self.chat = type("C", (), {"completions": _Completions(seq)})()


# 7) Robustesse LLM : une 1re réponse VIDE (modèle qui "réfléchit" trop) doit
#    déclencher le repli de format et finir par renvoyer du JSON, sans planter.
def t_llm_fallback():
    seq = ["", '{"headline":"h","summary":"s","items":[]}']  # vide puis JSON valide
    o1, o2 = research._provider_chain, research._client_for
    research._provider_chain = lambda: ["gemini"]
    research._client_for = lambda prov: (_Client(seq), "m")
    try:
        d = research._llm_json("réponds en json", "données")
    finally:
        research._provider_chain, research._client_for = o1, o2
    assert d["headline"] == "h", "le repli après réponse vide n'a pas fonctionné"


# 7bis) Basculement automatique : si le 1er fournisseur est à court de quota
#       (429), on doit passer au suivant et réussir — sans lever d'erreur.
def t_provider_failover():
    from openai import RateLimitError
    class Resp429:
        status_code = 429
    quota_err = RateLimitError("quota", response=type("R", (), {
        "status_code": 429, "request": None, "headers": {},
        "json": lambda self=None: {}})(), body=None)

    clients = {
        "gemini": _Client([quota_err]),                              # à court
        "groq": _Client(['{"headline":"ok","summary":"s","items":[]}']),  # prend le relais
    }
    o1, o2 = research._provider_chain, research._client_for
    research._provider_chain = lambda: ["gemini", "groq"]
    research._client_for = lambda prov: (clients[prov], "m")
    try:
        d = research._llm_json("json", "data")
    finally:
        research._provider_chain, research._client_for = o1, o2
    assert d["headline"] == "ok", "le basculement Gemini→Groq n'a pas fonctionné"


# 8) Décodage d'un 429 : on doit savoir dire "par minute" vs "par jour".
def t_quota_hint():
    err_day = type("E", (), {"body": {"error": {"details": [
        {"@type": ".../QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
        {"@type": ".../RetryInfo", "retryDelay": "34s"}]}}})()
    h = research._quota_hint(err_day)
    assert "PAR JOUR" in h and "34s" in h, h

    err_min = type("E", (), {"body": {"error": {"details": [
        {"@type": ".../QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]}]}}})()
    assert "PAR MINUTE" in research._quota_hint(err_min)

    # Corps illisible -> pas de plantage, juste vide.
    assert research._quota_hint(type("E", (), {"body": None})()) == ""

    # 413 "trop volumineux" doit être distingué (par code ET par message).
    assert research._is_too_large(type("E", (), {"status_code": 413})())
    assert research._is_too_large(RuntimeError("Request too large for model ... please reduce your message size"))
    assert not research._is_too_large(RuntimeError("banale erreur réseau"))


# 9) Extraction de la version corrigée (OSV + NVD)
def t_fixed_versions():
    osv_vuln = {"affected": [{"package": {"name": "requests"}, "ranges": [
        {"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "2.32.0"}]}]}]}
    assert research._osv_fixed_versions(osv_vuln, "requests") == ["2.32.0"]
    # un paquet différent ne doit pas polluer
    assert research._osv_fixed_versions(osv_vuln, "autre") == []

    nvd_cve = {"configurations": [{"nodes": [{"cpeMatch": [
        {"criteria": "cpe:2.3:o:apple:ios", "versionEndExcluding": "17.2"}]}]}]}
    assert research._nvd_fixed_versions(nvd_cve) == ["17.2"]
    assert research._nvd_fixed_versions({}) == []


# 10) Scheduler : activation par sujet + logique "est dû" selon la fréquence.
def t_scheduler():
    import app  # n'active PAS le thread (uniquement via __main__)
    from topics import Topic
    store.init_db()
    tp = Topic(id="sch", name="S", source="web", frequency_hours=48, feeds=["u"])
    store.upsert_topic(tp)
    assert store.is_enabled("sch") is True, "un sujet doit être activé par défaut"
    store.set_enabled("sch", False)
    assert store.is_enabled("sch") is False
    store.set_enabled("sch", True)

    with store._conn() as c:
        c.execute("DELETE FROM runs WHERE topic_id='sch'")
    assert app._is_due(tp) is True, "jamais lancé -> dû"
    store.save_run("sch", "", {"items": []})
    assert app._is_due(tp) is False, "vient de tourner -> pas dû"


# 11) Actu auto (Google News) : URL construite + pipeline sans lien fourni.
def t_news():
    from topics import Topic
    url = research._gnews_url("réalité augmentée")
    assert "news.google.com/rss/search" in url and "q=" in url, url

    of, ol = research._fetch_feed_items, research._llm_json
    research._fetch_feed_items = lambda feeds, since, max_items=25: [
        {"title": "A", "summary": "s", "link": "https://x/1",
         "published": "2026-08-02T10:00:00+00:00", "source": "S"}]
    research._llm_json = lambda s, u: {"headline": "h", "summary": "s", "items": [
        {"title": "t", "body": "b", "importance": "low",
         "sources": [{"title": "S", "url": "https://x/1"}]}]}
    try:
        t = Topic(id="n", name="Actu test", source="news", frequency_hours=24,
                  keywords=["réalité augmentée"])
        d = research.research(t)
    finally:
        research._fetch_feed_items, research._llm_json = of, ol
    assert len(d["items"]) == 1 and d["_item_keys"] == ["https://x/1"], d


# 12) Notifications : compteur de news non lues par sujet.
def t_unread():
    store.init_db()
    tid = "unreadtest"
    with store._conn() as c:
        c.execute("DELETE FROM feed_items WHERE topic_id=?", (tid,))
    store.set_setting(f"READ_ID:{tid}", "0")

    store.add_feed_items(tid, [{"title": "A", "body": "b", "importance": "low",
                                "sources": [{"url": "https://a"}]}])
    store.add_feed_items(tid, [{"title": "B", "body": "b", "importance": "low",
                                "sources": [{"url": "https://b"}]}])
    assert store.unread_count(tid) == 2, "2 non lus attendus"
    store.mark_read(tid)
    assert store.unread_count(tid) == 0, "0 après lecture"
    store.add_feed_items(tid, [{"title": "C", "body": "b", "importance": "low",
                                "sources": [{"url": "https://c"}]}])
    assert store.unread_count(tid) == 1, "1 nouveau non lu après un run"


# 13) Multi-utilisateurs : isolation des espaces, réglages par-user, invitations.
def t_accounts():
    from topics import Topic
    store.init_db()
    with store._conn() as c:
        for tb in ("users", "credentials", "invites"):
            c.execute(f"DELETE FROM {tb}")
        c.execute("DELETE FROM topics WHERE id IN ('a_top','b_top')")
    a = store.create_user("A", "Alice", True)
    b = store.create_user("B", "Bob", False)
    assert a["is_admin"] == 1 and b["is_admin"] == 0
    store.upsert_topic(Topic(id="a_top", name="A", source="web", frequency_hours=24,
                             feeds=["x"], owner_id="A"))
    store.upsert_topic(Topic(id="b_top", name="B", source="web", frequency_hours=24,
                             feeds=["y"], owner_id="B"))
    # Isolation : Alice ne voit/atteint pas le sujet de Bob.
    assert [t.id for t in store.list_topics("A")] == ["a_top"]
    assert store.get_topic("b_top", "B").owner_id == "B"
    try:
        store.get_topic("b_top", "A"); raise AssertionError("isolation KO")
    except KeyError:
        pass
    # Réglages par-utilisateur (clés perso, cloisonnées).
    store.set_user_setting("A", "GEMINI_API_KEY", "KA")
    store.set_user_setting("B", "GEMINI_API_KEY", "KB")
    assert store.get_user_setting("A", "GEMINI_API_KEY") == "KA"
    assert store.get_user_setting("B", "GEMINI_API_KEY") == "KB"
    # Invitations : usage unique.
    store.create_invite("tok", "A", "test")
    assert store.invite_is_valid("tok")
    store.consume_invite("tok", "B")
    assert not store.invite_is_valid("tok")
    # Passkeys : stockage + mise à jour du compteur anti-rejeu.
    store.add_credential("cred", "A", b"PUB", 0, "internal")
    assert store.get_credential("cred")["user_id"] == "A"
    store.update_sign_count("cred", 7)
    assert store.get_credential("cred")["sign_count"] == 7
    # Suppression d'un compte = suppression de son espace.
    store.delete_user("B")
    assert store.get_user("B") is None and store.list_topics("B") == []


# 14) Reprise du legacy : les sujets sans propriétaire reviennent au 1er admin.
def t_claim_legacy():
    from topics import Topic
    store.init_db()
    with store._conn() as c:
        c.execute("DELETE FROM topics")
        c.execute("DELETE FROM users")
    store.upsert_topic(Topic(id="legacy1", name="L", source="web",
                             frequency_hours=24, feeds=["x"]))  # owner_id None
    store.create_user("ADM", "Admin", True)
    n = store.claim_legacy("ADM")
    assert n >= 1 and store.get_topic("legacy1", "ADM").owner_id == "ADM"


# 15) Clés par-utilisateur pendant un run : research._cfg lit le getter contextuel
#     (chaque sujet tourne avec la clé de SON propriétaire), sinon repli global.
def t_user_keys_context():
    assert research._cfg("MISSING_XYZ", "def") == "def"
    token = research.set_settings_getter(
        lambda k, d=None: {"GEMINI_API_KEY": "USERKEY"}.get(k))
    try:
        assert research._cfg("GEMINI_API_KEY") == "USERKEY"
        assert research._cfg("AUTRE", "d") == "d"   # pas de repli global en contexte user
    finally:
        research.reset_settings_getter(token)
    # Hors contexte : repli global/env.
    store.set_setting("GEMINI_API_KEY", "GLOBALKEY")
    assert research._cfg("GEMINI_API_KEY") == "GLOBALKEY"


# 16) Rôle admin : le décorateur @admin_required renvoie 403 à un non-admin.
def t_admin_required():
    import app
    from flask import session
    store.init_db()
    with store._conn() as c:
        c.execute("DELETE FROM users")
    store.create_user("ADM", "Admin", True)
    store.create_user("USR", "User", False)

    @app.admin_required
    def _protected():
        return "ok"

    with app.app.test_request_context("/"):
        session["uid"] = "USR"
        r = _protected()
        assert isinstance(r, tuple) and r[1] == 403, "un non-admin devrait recevoir 403"
    with app.app.test_request_context("/"):
        session["uid"] = "ADM"
        assert _protected() == "ok", "l'admin devrait passer"


print("Cardinal — self-test (hors-ligne)\n")
check("Chargement des sujets", t_topics)
check("Parsing RSS + fenêtre + nettoyage HTML", t_rss)
check("Parsing NVD (sévérité, langue, refs)", t_nvd)
check("Extraction JSON réponse LLM", t_json)
check("Rendu HTML + écriture SQLite", t_render_db)
check("Feed continu (accumulation, dédup, purge 1 mois)", t_feed)
check("Extraction version corrigée (OSV + NVD)", t_fixed_versions)
check("Robustesse LLM (repli si réponse vide)", t_llm_fallback)
check("Basculement automatique de fournisseur (429)", t_provider_failover)
check("Décodage du 429 (par minute / par jour)", t_quota_hint)
check("Scheduler (activation + logique 'est dû')", t_scheduler)
check("Actu auto (Google News, sans lien)", t_news)
check("Notifications (compteur non lus)", t_unread)
check("Comptes : isolation + réglages perso + invitations", t_accounts)
check("Reprise du legacy (sujets → 1er admin)", t_claim_legacy)
check("Clés par-utilisateur (contexte de run)", t_user_keys_context)
check("Rôle admin (@admin_required → 403)", t_admin_required)

# nettoyage
for f in (store.DB_PATH,):
    if os.path.exists(f):
        os.remove(f)

ok = sum(1 for _, p, _ in results if p)
print(f"\n{ok}/{len(results)} tests OK.")
if ok == len(results):
    print("Tuyauterie validée. Ajoute ta clé dans .env puis lance un vrai run "
          "(ex. python agent.py run gamedev).")
else:
    raise SystemExit(1)
