"""
Cardinal — dashboard web.

Lance :  python app.py
Puis ouvre http://127.0.0.1:5000 dans ton navigateur.

Tout se pilote depuis la page : voir les sujets, en ajouter, lancer une veille,
lire le digest. La clé LLM se colle dans "Réglages" (ou reste dans .env).
"""

import os
import time
import uuid
import threading
import subprocess
import datetime as dt

import requests

from flask import (
    Flask, request, redirect, url_for, jsonify, send_from_directory,
    render_template_string, abort, session,
)
from dotenv import load_dotenv

import store
import research
import render
from topics import Topic

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DIGESTS_DIR = os.path.join(BASE_DIR, "digests")
os.makedirs(DIGESTS_DIR, exist_ok=True)

# Chemins absolus -> l'appli tourne quel que soit le dossier courant
store.DB_PATH = os.path.join(BASE_DIR, "cardinal.db")

# Fuseau d'affichage explicite (sinon .astimezone() suit le fuseau serveur, souvent
# UTC en prod -> heures décalées). Configurable via CARDINAL_TZ. Défaut : Europe/Paris.
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.getenv("CARDINAL_TZ", "Europe/Paris"))
except Exception:
    TZ = None


def _to_local(d: dt.datetime) -> dt.datetime:
    return d.astimezone(TZ) if TZ else d.astimezone()


app = Flask(__name__)
store.init_db()

# --- Authentification (activée SEULEMENT si un mot de passe est défini) ----
# En local, sans CARDINAL_PASSWORD, aucun login n'est demandé. En production,
# on met CARDINAL_PASSWORD=... pour protéger l'accès (et donc tes clés/quotas).
CARDINAL_PASSWORD = os.getenv("CARDINAL_PASSWORD")
# Nom du service systemd à redémarrer depuis le bouton « Mettre à jour » (prod).
SERVICE_NAME = os.getenv("CARDINAL_SERVICE", "cardinal")
# URL publique (pour les liens cliquables dans les notifications push).
CARDINAL_URL = os.getenv("CARDINAL_URL", "https://cardinal.aguetai.fr")
# Clé de session stable (survit aux redémarrages) : env, sinon stockée en base.
app.secret_key = os.getenv("CARDINAL_SECRET_KEY") or store.get_setting("SECRET_KEY")
if not app.secret_key:
    app.secret_key = uuid.uuid4().hex
    store.set_setting("SECRET_KEY", app.secret_key)
app.permanent_session_lifetime = dt.timedelta(days=30)


@app.before_request
def _require_login():
    if not CARDINAL_PASSWORD or session.get("auth"):
        return  # mode local ouvert, ou déjà connecté
    p = request.path
    if p == "/login" or p == "/healthz" or p.startswith("/img/"):
        return
    if p.startswith("/api/"):
        return ("Non authentifié", 401)
    return redirect(url_for("login"))

# --- Jobs en tâche de fond (mémoire) --------------------------------------
JOBS = {}
JOBS_LOCK = threading.Lock()
# Un seul run à la fois (manuel OU scheduler) : évite de marteler les APIs
# gratuites en parallèle (429) et les écritures SQLite concurrentes.
RUN_LOCK = threading.Lock()


def _set_job(job_id, **data):
    with JOBS_LOCK:
        JOBS[job_id] = {**JOBS.get(job_id, {}), **data}


# --- Notifications push (ntfy.sh) -----------------------------------------
def _ntfy_target():
    topic = (store.get_setting("NTFY_TOPIC", "") or "").strip()
    server = (store.get_setting("NTFY_SERVER", "") or "https://ntfy.sh").strip().rstrip("/")
    return server, topic


_NTFY_PRIO = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5, "urgent": 5}


def _send_ntfy(title, body, click=None, priority="default", tags=None, server=None, topic=None):
    """Envoie une notif push via ntfy en publiant du JSON (corps UTF-8) : le titre
    ET le corps peuvent donc porter des accents/caractères non-ASCII (les en-têtes
    HTTP, eux, ne gèrent pas l'UTF-8 → titres en « � »). -> (ok, msg).
    `topic`/`server` explicites (test avant enregistrement) ou lus des Réglages."""
    if topic is None:
        server, topic = _ntfy_target()
    topic = (topic or "").strip()
    server = (server or "https://ntfy.sh").strip().rstrip("/")
    if not topic:
        return False, "ntfy non configuré (topic vide dans Réglages)"
    payload = {"topic": topic, "title": title, "message": body,
               "priority": _NTFY_PRIO.get(priority, 3)}
    if tags:
        payload["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if click:
        payload["click"] = click
    try:
        r = requests.post(server, json=payload, timeout=10)  # POST racine + JSON
        r.raise_for_status()
        return True, "ok"
    except Exception as e:
        return False, str(e)


def _notify_new_high(topic, high_items):
    """Push résumant les nouveautés de priorité HAUTE ajoutées lors d'un run.
    Le libellé s'adapte à la source : « CVE » pour les sujets vulnérabilités
    (OSV/NVD/KEV), « nouveauté » pour les sujets d'actu/RSS (ex. Sword Art Online)."""
    n = len(high_items)
    suf = "s" if n > 1 else ""
    noun = f"{n} CVE prioritaire{suf}" if topic.source in ("osv", "nvd", "kev") \
        else f"{n} nouveauté{suf} prioritaire{suf}"
    lines = [f"• {it.get('title', '')}" for it in high_items[:5]]
    if n > 5:
        lines.append(f"… +{n - 5} autre(s)")
    _send_ntfy(f"Cardinal · {topic.name} : {noun}", "\n".join(lines),
               click=f"{CARDINAL_URL}/feed/{topic.id}", priority="high", tags="rotating_light")


def _core_run(topic) -> int:
    """Exécute une veille et l'empile dans le feed. Partagé par le bouton
    « Lancer » et le scheduler. Renvoie le nombre de nouveaux items."""
    digest = research.research(topic)
    keys = digest.pop("_item_keys", None)
    items = digest.get("items", [])
    new_items = store.add_feed_items(topic.id, items) if items else []
    if keys:
        store.mark_seen(topic.id, keys)
    store.save_run(topic.id, "", digest)   # garde l'horodatage du dernier run
    store.purge_feed()                     # fenêtre glissante : retire ce qui a > 1 mois
    highs = [it for it in new_items if it.get("importance") == "high"]
    if highs:
        try:
            _notify_new_high(topic, highs)     # push : seulement les nouveautés prioritaires
        except Exception as e:
            print(f"[notify] échec : {e}")
    return len(new_items)


def _run_job(job_id, topic_id):
    try:
        topic = store.get_topic(topic_id)
        with RUN_LOCK:
            added = _core_run(topic)
        _set_job(job_id, status="done", added=added,
                 total=store.feed_count(topic.id))
    except Exception as e:
        _set_job(job_id, status="error", error=str(e))


# --- Scheduler : lance les sujets tout seuls selon leur fréquence ----------
SCHED_INTERVAL = 60      # secondes entre deux vérifications
SCHED_COOLDOWN = 30 * 60  # après une TENTATIVE auto, on ne réessaie pas ce sujet
                          # avant 30 min (évite de marteler un sujet qui échoue)
_last_attempt = {}        # topic_id -> time.monotonic() de la dernière tentative auto


def _is_due(topic) -> bool:
    last = store.latest_run(topic.id)
    if not last:
        return True  # jamais lancé -> dû dès l'activation
    last_dt = dt.datetime.fromisoformat(last["created_at"])
    return dt.datetime.now(dt.timezone.utc) - last_dt >= dt.timedelta(hours=topic.frequency_hours)


def _scheduler_loop():
    while True:
        try:
            if store.get_setting("SCHEDULER_ENABLED", "1") == "1":
                for topic in store.list_topics():
                    if not (store.is_enabled(topic.id) and _is_due(topic)):
                        continue
                    # backoff : pas de nouvelle tentative auto trop rapprochée,
                    # même si le sujet reste "dû" (échec = aucun run enregistré).
                    if time.monotonic() - _last_attempt.get(topic.id, 0) < SCHED_COOLDOWN:
                        continue
                    # verrou NON bloquant : si un run manuel est en cours, on
                    # laisse la main et on réessaiera au prochain cycle.
                    if not RUN_LOCK.acquire(blocking=False):
                        break
                    try:
                        _last_attempt[topic.id] = time.monotonic()
                        n = _core_run(topic)
                        print(f"[scheduler] {topic.id} : {n} nouveau(x)")
                    except Exception as e:
                        print(f"[scheduler] {topic.id} échec (nouvelle tentative dans "
                              f"{SCHED_COOLDOWN // 60} min) : {e}")
                    finally:
                        RUN_LOCK.release()
        except Exception as e:
            print(f"[scheduler] boucle : {e}")
        time.sleep(SCHED_INTERVAL)


def start_scheduler():
    threading.Thread(target=_scheduler_loop, daemon=True).start()


# --- Helpers vue ----------------------------------------------------------
SOURCE_LABEL = {"news": "Actu (auto)", "web": "RSS (liens)",
                "osv": "CVE dépendances", "nvd": "CVE produits",
                "kev": "CVE exploitées (KEV)"}


def _summary(t: Topic) -> str:
    if t.source == "news":
        return f"{len(t.keywords)} sujet(s) suivis"
    if t.source == "web":
        return f"{len(t.feeds)} flux RSS"
    if t.source == "osv":
        return f"{len(t.packages)} paquet(s)"
    if t.source == "nvd":
        s = f"{len(t.keywords)} produit(s)"
        return s + (f" · ≥ {t.min_severity}" if t.min_severity else "")
    if t.source == "kev":
        return f"{len(t.keywords)} produit(s) · exploitées"
    return ""


def _next_run_ts(t: Topic) -> int:
    """Horodatage (epoch, UTC) du prochain run auto — pour le compte à rebours.
    Jamais lancé -> maintenant (dû au prochain cycle)."""
    last = store.latest_run(t.id)
    now = dt.datetime.now(dt.timezone.utc)
    if not last:
        return int(now.timestamp())
    nxt = dt.datetime.fromisoformat(last["created_at"]) + dt.timedelta(hours=t.frequency_hours)
    return int(nxt.timestamp())


def _config_text(t: Topic) -> str:
    """Le contenu éditable, une entrée par ligne, selon la source."""
    if t.source in ("nvd", "news", "kev"):
        return "\n".join(t.keywords)
    if t.source == "web":
        return "\n".join(t.feeds)
    if t.source == "osv":
        return "\n".join(f"{p['name']} {p['ecosystem']}" for p in t.packages)
    return ""


def _parse_config(source, text, min_severity=""):
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    cfg = {"feeds": [], "packages": [], "keywords": [], "min_severity": ""}
    if source == "web":
        cfg["feeds"] = lines
    elif source == "news":
        cfg["keywords"] = lines
    elif source == "nvd":
        cfg["keywords"] = lines
        cfg["min_severity"] = min_severity or ""
    elif source == "kev":
        cfg["keywords"] = lines
    elif source == "osv":
        for l in lines:
            parts = l.split()
            if len(parts) >= 2:
                cfg["packages"].append({"name": parts[0], "ecosystem": parts[1]})
    return cfg


# --- Routes ---------------------------------------------------------------
@app.route("/")
def index():
    topics = store.list_topics()
    sched_on = store.get_setting("SCHEDULER_ENABLED", "1") == "1"
    cards = []
    for t in topics:
        last = store.latest_run(t.id)
        enabled = store.is_enabled(t.id)
        cards.append({
            "t": t,
            "label": SOURCE_LABEL.get(t.source, t.source),
            "summary": _summary(t),
            "config_text": _config_text(t),
            "feed_n": store.feed_count(t.id),
            "unread": store.unread_count(t.id),
            "last_at": _to_local(dt.datetime.fromisoformat(last["created_at"])).strftime("%d/%m %H:%M") if last else None,
            "enabled": enabled,
            "next_ts": _next_run_ts(t) if (enabled and sched_on) else None,
        })
    provider = store.get_setting("LLM_PROVIDER", os.getenv("LLM_PROVIDER", "gemini"))
    # Une clé par fournisseur : sert au basculement automatique en cas de quota.
    keys = {p: bool(research._provider_key(p)) for p in research.PROVIDERS}
    return render_template_string(
        PAGE, cards=cards, provider=provider, keys=keys, has_key=any(keys.values()),
        sched_on=sched_on, auth_on=bool(CARDINAL_PASSWORD),
        or_model=research._cfg("OPENROUTER_MODEL", "") or "",
        or_model_default=research.PROVIDERS["openrouter"]["model"],
        has_nvd=bool(store.get_setting("NVD_API_KEY") or os.getenv("NVD_API_KEY")),
        ntfy_topic=store.get_setting("NTFY_TOPIC", "") or "",
        ntfy_server=store.get_setting("NTFY_SERVER", "") or "",
    )


@app.route("/topics/add", methods=["POST"])
def add_topic():
    f = request.form
    tid = (f.get("id") or "").strip().lower().replace(" ", "-")
    if not tid or not f.get("name"):
        return redirect(url_for("index"))
    cfg = _parse_config(f.get("source"), f.get("config"), f.get("min_severity"))
    t = Topic(
        id=tid, name=f.get("name").strip(), source=f.get("source"),
        frequency_hours=int(f.get("frequency_hours") or 24),
        feeds=cfg["feeds"], packages=cfg["packages"],
        keywords=cfg["keywords"], min_severity=cfg["min_severity"],
    )
    store.upsert_topic(t)
    return redirect(url_for("index"))


@app.route("/topics/<tid>/edit", methods=["POST"])
def edit_topic(tid):
    """Met à jour un sujet existant (l'identifiant reste figé)."""
    f = request.form
    name = (f.get("name") or "").strip()
    source = f.get("source")
    if not name or not source:
        return redirect(url_for("index"))
    cfg = _parse_config(source, f.get("config"), f.get("min_severity"))
    t = Topic(
        id=tid, name=name, source=source,
        frequency_hours=int(f.get("frequency_hours") or 24),
        feeds=cfg["feeds"], packages=cfg["packages"],
        keywords=cfg["keywords"], min_severity=cfg["min_severity"],
    )
    store.upsert_topic(t)
    return redirect(url_for("index"))


@app.route("/topics/<tid>/delete", methods=["POST"])
def delete_topic(tid):
    store.delete_topic(tid)
    return redirect(url_for("index"))


@app.route("/topics/<tid>/toggle", methods=["POST"])
def toggle_topic(tid):
    """Active / met en pause la veille automatique d'un sujet."""
    store.set_enabled(tid, not store.is_enabled(tid))
    return redirect(url_for("index"))


@app.route("/settings", methods=["POST"])
def save_settings():
    f = request.form
    store.set_setting("LLM_PROVIDER", f.get("provider", "gemini"))
    # Une clé par fournisseur (on n'écrase jamais avec du vide).
    for p, cfg in research.PROVIDERS.items():
        val = (f.get(p + "_key") or "").strip()
        if val:
            store.set_setting(cfg["key_env"], val)
    if f.get("nvd_key"):
        store.set_setting("NVD_API_KEY", f.get("nvd_key").strip())
    # Modèle OpenRouter (les modèles gratuits changent ; vide = défaut).
    store.set_setting("OPENROUTER_MODEL", (f.get("openrouter_model") or "").strip())
    # Veille automatique globale (case cochée = "on").
    store.set_setting("SCHEDULER_ENABLED", "1" if f.get("scheduler") else "0")
    # Notifications push ntfy (topic vide = désactivé ; serveur vide = ntfy.sh).
    store.set_setting("NTFY_TOPIC", (f.get("ntfy_topic") or "").strip())
    store.set_setting("NTFY_SERVER", (f.get("ntfy_server") or "").strip())
    return redirect(url_for("index"))


@app.route("/api/notify-test", methods=["POST"])
def api_notify_test():
    data = request.get_json(silent=True) or {}
    ok, msg = _send_ntfy(
        "Cardinal: test", "Notification de test — si tu reçois ceci, c'est bon !",
        click=CARDINAL_URL, priority="default", tags="white_check_mark",
        topic=(data.get("topic") or None), server=(data.get("server") or None))
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/run/<tid>", methods=["POST"])
def api_run(tid):
    job_id = uuid.uuid4().hex
    _set_job(job_id, status="running")
    threading.Thread(target=_run_job, args=(job_id, tid), daemon=True).start()
    return jsonify({"job": job_id})


@app.route("/api/status/<job_id>")
def api_status(job_id):
    with JOBS_LOCK:
        return jsonify(JOBS.get(job_id, {"status": "unknown"}))


def _delayed_restart():
    """Redémarre le service APRÈS que la réponse HTTP soit partie (sinon on tue
    le process en plein envoi). --no-block : systemd (PID 1) mène le restart même
    si ce client meurt avec le service."""
    time.sleep(1.5)
    try:
        subprocess.Popen(["systemctl", "restart", "--no-block", SERVICE_NAME],
                         start_new_session=True)
    except Exception as e:
        print(f"[update] restart échec : {e}")


@app.route("/api/update", methods=["POST"])
def api_update():
    # Action puissante (git + restart en root) : on l'interdit tant qu'aucun mot
    # de passe ne protège l'accès, pour qu'une instance ouverte ne soit pas pilotable.
    if not CARDINAL_PASSWORD:
        return jsonify({"ok": False, "output": "Indisponible : définis CARDINAL_PASSWORD."}), 403

    def _git(*args):
        return subprocess.run(["git", *args], cwd=BASE_DIR, capture_output=True,
                              text=True, timeout=60)

    try:
        before = _git("rev-parse", "HEAD").stdout.strip()
        pull = _git("pull", "--ff-only")
        after = _git("rev-parse", "HEAD").stdout.strip()
    except Exception as e:
        return jsonify({"ok": False, "output": f"Échec git : {e}"}), 500

    output = (pull.stdout + pull.stderr).strip() or "(pas de sortie)"
    if pull.returncode != 0:
        return jsonify({"ok": False, "output": output})

    changed = bool(before) and bool(after) and before != after
    if changed:
        threading.Thread(target=_delayed_restart, daemon=True).start()
    return jsonify({"ok": True, "output": output, "restarting": changed})


RECAP_MAX_DAYS = 30  # on ne remonte pas au-delà de la rétention du feed
SOURCE_RANK = {"news": 0, "web": 1, "nvd": 2, "osv": 3}


def _first_sentence(text: str, maxlen: int = 200) -> str:
    """Première phrase du résumé, pour donner un contexte court dans le récap."""
    t = " ".join((text or "").split())
    if not t:
        return ""
    end = len(t)
    for i, ch in enumerate(t):
        if ch in ".!?" and (i + 1 >= len(t) or t[i + 1] == " "):
            end = i + 1
            break
    s = t[:end]
    if len(s) > maxlen:                       # phrase trop longue -> on coupe proprement
        s = s[:maxlen].rsplit(" ", 1)[0] + "…"
    return s


def _day_range(offset: int):
    """Bornes UTC [start, end[ du jour local à `offset` jours en arrière, + libellé."""
    now_local = _to_local(dt.datetime.now(dt.timezone.utc))
    target = (now_local - dt.timedelta(days=offset)).date()
    start_local = dt.datetime(target.year, target.month, target.day, tzinfo=now_local.tzinfo)
    end_local = start_local + dt.timedelta(days=1)
    label = "Aujourd'hui" if offset == 0 else ("Hier" if offset == 1
                                               else target.strftime("%d/%m/%Y"))
    return (start_local.astimezone(dt.timezone.utc).isoformat(),
            end_local.astimezone(dt.timezone.utc).isoformat(), label)


@app.route("/api/recap/<int:offset>")
def api_recap(offset):
    """News d'un jour, regroupées par sujet (les sujets sans news ne sont pas listés)."""
    offset = max(0, min(offset, RECAP_MAX_DAYS))
    start, end, label = _day_range(offset)
    rows = store.feed_items_between(start, end)
    topics = {t.id: t for t in store.list_topics()}

    groups = {}
    for r in rows:
        t = topics.get(r["topic_id"])
        if not t:
            continue  # sujet supprimé depuis
        g = groups.setdefault(t.id, {
            "topic_id": t.id, "name": t.name, "source": t.source,
            "label": SOURCE_LABEL.get(t.source, t.source), "items": [],
        })
        g["items"].append({"title": r["title"], "fix": r["fix"] or "",
                           "context": _first_sentence(r["body"]), "sources": r["sources"]})

    ordered = sorted(groups.values(), key=lambda g: (SOURCE_RANK.get(g["source"], 9), g["name"]))
    return jsonify({
        "offset": offset, "label": label,
        "can_prev": offset < RECAP_MAX_DAYS,
        "can_next": offset > 0,
        "groups": ordered,
    })


@app.route("/api/dashboard")
def api_dashboard():
    """État léger de chaque sujet, pour rafraîchir le dashboard sans recharger."""
    sched_on = store.get_setting("SCHEDULER_ENABLED", "1") == "1"
    out = {}
    for t in store.list_topics():
        enabled = store.is_enabled(t.id)
        out[t.id] = {
            "unread": store.unread_count(t.id),
            "feed_n": store.feed_count(t.id),
            "next_ts": _next_run_ts(t) if (enabled and sched_on) else None,
        }
    return jsonify(out)


@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/login", methods=["GET", "POST"])
def login():
    if not CARDINAL_PASSWORD:
        return redirect(url_for("index"))
    error = ""
    if request.method == "POST":
        if (request.form.get("password") or "") == CARDINAL_PASSWORD:
            session["auth"] = True
            session.permanent = True
            return redirect(url_for("index"))
        error = "Mot de passe incorrect."
    return render_template_string(LOGIN_PAGE, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/img/<path:filename>")
def img(filename):
    return send_from_directory(os.path.join(BASE_DIR, "img"), filename)


@app.route("/feed/<tid>")
def feed(tid):
    try:
        topic = store.get_topic(tid)
    except KeyError:
        abort(404)
    items = store.get_feed(tid)
    store.mark_read(tid)   # ouvrir le feed = tout marquer comme lu
    return render.render_feed(topic, items, has_run=bool(store.latest_run(tid)))


@app.route("/digests/<path:filename>")
def digests(filename):
    # Conservé pour les anciens fichiers HTML produits par la CLI.
    if not os.path.exists(os.path.join(DIGESTS_DIR, filename)):
        abort(404)
    return send_from_directory(DIGESTS_DIR, filename)


# --- Page de connexion (production) ---------------------------------------
LOGIN_PAGE = r"""<!doctype html><html lang="fr"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cardinal — connexion</title>
<link rel="icon" type="image/png" href="/img/favicon.png">
<style>
 *{box-sizing:border-box}
 body{margin:0;min-height:100vh;display:grid;place-items:center;color:#f3e9cf;
   font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
   background:radial-gradient(900px 480px at 50% -10%,rgba(230,184,74,.14),transparent 60%),#0a0906}
 .box{width:min(360px,calc(100% - 32px));background:#141009;border:1px solid rgba(230,184,74,.28);
   border-radius:14px;padding:30px 26px;box-shadow:0 0 44px rgba(230,184,74,.14);text-align:center}
 img{width:64px;height:64px;object-fit:contain;filter:drop-shadow(0 0 14px rgba(230,184,74,.55))}
 h1{font-size:1.3rem;letter-spacing:.12em;text-transform:uppercase;margin:12px 0 2px}
 .sub{font-family:ui-monospace,Menlo,monospace;font-size:11px;letter-spacing:.2em;text-transform:uppercase;
   color:#b49a63;margin-bottom:22px}
 input{width:100%;padding:11px 13px;border:1px solid rgba(230,184,74,.28);border-radius:8px;
   background:rgba(0,0,0,.35);color:#f3e9cf;font:inherit}
 input:focus{outline:none;border-color:#e6b84a;box-shadow:0 0 12px rgba(230,184,74,.2)}
 button{width:100%;margin-top:12px;padding:11px;border:0;border-radius:8px;cursor:pointer;font-weight:700;
   color:#1a1405;background:linear-gradient(180deg,#f2cf6a,#d99f2c);box-shadow:0 0 16px rgba(230,184,74,.4)}
 .err{color:#e2685f;font-size:13px;margin-top:12px;min-height:16px}
</style></head><body>
 <form class="box" method="post">
   <img src="/img/cardinal-icon.png" alt="Cardinal">
   <h1>Cardinal</h1>
   <div class="sub">Accès protégé</div>
   <input type="password" name="password" placeholder="Mot de passe" autofocus autocomplete="current-password">
   <button type="submit">Entrer</button>
   <div class="err">{{ error }}</div>
 </form>
</body></html>"""


# --- Template (une seule page) --------------------------------------------
PAGE = r"""<!doctype html><html lang="fr"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cardinal — veille</title>
<link rel="icon" type="image/png" href="/img/favicon.png">
<link rel="apple-touch-icon" href="/img/cardinal-icon.png">
<style>
 /* DA façon HUD "Cardinal / ARGUS" (Sword Art Online) : or sur noir, lignes fines, lueurs. */
 :root{
   --ink:#f3e9cf;--muted:#b49a63;--faint:#7c6836;--line:rgba(230,184,74,.28);--line2:rgba(230,184,74,.09);
   --accent:#e6b84a;--accent-ink:#f6d67f;--gold:#e6b84a;--bg:#0a0906;--card:rgba(24,20,11,.72);
   --shadow:0 0 0 1px rgba(230,184,74,.05),0 10px 30px rgba(0,0,0,.55);
   --web:#e6a53a;--web-bg:rgba(230,165,58,.15);--osv:#4fd6a6;--osv-bg:rgba(79,214,166,.14);
   --nvd:#e0b877;--nvd-bg:rgba(224,184,119,.15);--news:#7fb6ff;--news-bg:rgba(127,182,255,.15);
 }
 *{box-sizing:border-box}
 ::selection{background:rgba(230,184,74,.28)}
 body{margin:0;color:var(--ink);line-height:1.55;-webkit-font-smoothing:antialiased;
   font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;min-height:100vh;
   background:
     radial-gradient(1100px 520px at 50% -8%, rgba(230,184,74,.13), transparent 60%),
     linear-gradient(rgba(230,184,74,.035) 1px, transparent 1px) 0 0/44px 44px,
     linear-gradient(90deg, rgba(230,184,74,.035) 1px, transparent 1px) 0 0/44px 44px,
     #0a0906;}
 .wrap{max-width:980px;margin:0 auto;padding:34px 20px 96px}
 header{display:flex;align-items:center;gap:16px;margin-bottom:4px}
 .mark{width:58px;height:58px;flex:none;display:grid;place-items:center}
 .mark img{width:100%;height:100%;object-fit:contain;filter:drop-shadow(0 0 14px rgba(230,184,74,.55))}
 h1{font-size:1.7rem;margin:0;letter-spacing:.09em;line-height:1.05;text-transform:uppercase;font-weight:800;
   text-shadow:0 0 20px rgba(230,184,74,.4)}
 .sub{color:var(--muted);font-size:11.5px;margin-top:4px;letter-spacing:.24em;text-transform:uppercase;
   font-family:ui-monospace,Menlo,monospace}
 .tag{color:var(--muted);font-size:13px}
 .lead{color:var(--muted);font-size:14px;margin:14px 0 0}
 .banner{background:var(--web-bg);border:1px solid var(--line);color:var(--ink);padding:12px 15px;
   border-radius:10px;margin:18px 0 0;font-size:14px}
 /* Récap du jour */
 .recap{margin-top:26px;background:var(--card);border:1px solid var(--line);border-radius:14px;
   padding:14px 20px 20px;box-shadow:var(--shadow);backdrop-filter:blur(3px);position:relative}
 .recap::before{content:"";position:absolute;top:7px;left:7px;width:14px;height:14px;
   border-top:2px solid var(--gold);border-left:2px solid var(--gold);opacity:.55}
 .recap-nav{display:flex;align-items:center;justify-content:space-between;gap:12px;
   border-bottom:1px solid var(--line);padding-bottom:12px}
 .recap-day{font-family:ui-monospace,Menlo,monospace;text-transform:uppercase;letter-spacing:.16em;
   font-weight:700;color:var(--gold);font-size:14px}
 .recap-arrow{font-family:ui-monospace,Menlo,monospace;font-size:12px;letter-spacing:.05em}
 .recap-arrow:disabled{opacity:.3;cursor:default;border-color:var(--line);box-shadow:none}
 .recap-group{margin-top:16px}
 .recap-gh{display:flex;align-items:center;gap:10px;margin-bottom:9px;font-weight:700;color:#fbf3df;font-size:15px}
 .recap-body ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px}
 .recap-body li{font-size:14px;line-height:1.45;padding-left:17px;position:relative;color:var(--muted)}
 .recap-body li::before{content:"›";position:absolute;left:0;color:var(--gold);opacity:.75;font-weight:700}
 .recap-body li a{color:var(--ink);text-decoration:none;border-bottom:1px solid transparent}
 .recap-body li a:hover{color:var(--gold);border-bottom-color:var(--line)}
 .recap-fix{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--osv);margin-left:7px;overflow-wrap:anywhere}
 .recap-ctx{color:var(--faint);font-size:13px;line-height:1.45;margin-top:2px}
 .recap-empty{color:var(--muted);font-size:14px;padding:16px 0 4px;font-style:italic}
 .section-h{display:flex;align-items:center;gap:12px;margin:34px 0 16px}
 .section-h h2{font-size:.72rem;text-transform:uppercase;letter-spacing:.28em;color:var(--gold);margin:0;
   font-weight:700;font-family:ui-monospace,Menlo,monospace}
 .section-h .line{flex:1;height:1px;background:linear-gradient(90deg,var(--line),transparent)}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(285px,1fr));gap:18px}
 .card{position:relative;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;
   display:flex;flex-direction:column;gap:9px;box-shadow:var(--shadow);backdrop-filter:blur(3px);
   transition:border-color .16s,box-shadow .16s,transform .16s}
 .card:hover{border-color:var(--gold);box-shadow:0 0 24px rgba(230,184,74,.18);transform:translateY(-1px)}
 .card::before{content:"";position:absolute;top:7px;left:7px;width:14px;height:14px;
   border-top:2px solid var(--gold);border-left:2px solid var(--gold);opacity:.6}
 .card::after{content:"";position:absolute;bottom:7px;right:7px;width:14px;height:14px;
   border-bottom:2px solid var(--gold);border-right:2px solid var(--gold);opacity:.35}
 .unread{position:absolute;top:-9px;right:-9px;min-width:24px;height:24px;padding:0 7px;border-radius:12px;
   background:#e2342f;color:#fff;font-size:12.5px;font-weight:800;display:inline-flex;align-items:center;
   justify-content:center;box-shadow:0 0 12px rgba(226,52,47,.8);border:2px solid #0a0906;z-index:2}
 .badge{align-self:flex-start;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.12em;
   padding:4px 11px;border-radius:4px;background:var(--nvd-bg);color:var(--nvd);border:1px solid var(--line);
   font-family:ui-monospace,Menlo,monospace}
 .badge.b-news{background:var(--news-bg);color:var(--news)}
 .badge.b-web{background:var(--web-bg);color:var(--web)}
 .badge.b-osv{background:var(--osv-bg);color:var(--osv)}
 .badge.b-nvd{background:var(--nvd-bg);color:var(--nvd)}
 .badge.b-kev{background:rgba(240,64,73,.16);color:#ff6b6b}
 .card h3{margin:2px 0 0;font-size:1.16rem;letter-spacing:.01em;color:#fbf3df}
 .card .meta{color:var(--muted);font-size:12.5px;font-family:ui-monospace,Menlo,monospace}
 .status{font-size:13px;color:var(--muted);min-height:18px}
 .auto{margin-top:2px}
 .pill{display:inline-block;white-space:nowrap;font-size:11.5px;color:var(--muted);background:var(--line2);
   border:1px solid var(--line);border-radius:5px;padding:3px 9px;font-family:ui-monospace,Menlo,monospace;letter-spacing:.03em}
 .pill.on{color:var(--gold);background:rgba(230,184,74,.10)}
 .row{display:flex;gap:8px;flex-wrap:wrap;margin-top:auto;padding-top:10px;align-items:center}
 button,.btn{font:inherit;border:1px solid var(--line);background:rgba(230,184,74,.04);border-radius:8px;
   padding:7px 13px;cursor:pointer;color:var(--ink);text-decoration:none;font-size:14px;
   transition:background .12s,border-color .12s,box-shadow .12s}
 button:hover,.btn:hover{border-color:var(--gold);box-shadow:0 0 10px rgba(230,184,74,.2)}
 .btn-go{background:linear-gradient(180deg,#f2cf6a,#d99f2c);color:#1a1405;border-color:#f2cf6a;font-weight:700;
   box-shadow:0 0 16px rgba(230,184,74,.4)}
 .btn-go:hover{filter:brightness(1.05);box-shadow:0 0 24px rgba(230,184,74,.6)}
 .btn-go:disabled{opacity:.6;cursor:default}
 .icon-btn{padding:7px 11px;line-height:1}
 .btn-del{color:#e2685f;border-color:transparent;background:transparent}
 .btn-del:hover{border-color:#e2685f;background:transparent;box-shadow:none}
 .push{margin-left:auto}
 .spin{display:inline-block;width:13px;height:13px;border:2px solid var(--line);border-top-color:var(--gold);
   border-radius:50%;animation:s .7s linear infinite;vertical-align:-2px;margin-right:6px}
 @keyframes s{to{transform:rotate(360deg)}}
 details.panel{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:4px 18px;
   margin-top:14px;box-shadow:var(--shadow);backdrop-filter:blur(3px)}
 summary{cursor:pointer;font-weight:600;padding:12px 0;list-style:none;display:flex;align-items:center;gap:8px;
   letter-spacing:.02em}
 summary::-webkit-details-marker{display:none}
 summary::before{content:"›";font-size:20px;color:var(--gold);transition:transform .15s;line-height:1}
 details[open] summary::before{transform:rotate(90deg)}
 label{display:block;font-size:12px;color:var(--muted);margin:12px 0 5px;font-weight:600;letter-spacing:.06em;
   text-transform:uppercase;font-family:ui-monospace,Menlo,monospace}
 input,select,textarea{width:100%;font:inherit;padding:9px 11px;border:1px solid var(--line);border-radius:8px;
   background:rgba(0,0,0,.35);color:var(--ink);transition:border-color .12s,box-shadow .12s}
 input:focus,select:focus,textarea:focus{outline:none;border-color:var(--gold);box-shadow:0 0 12px rgba(230,184,74,.2)}
 input:disabled{opacity:.6;cursor:not-allowed}
 textarea{min-height:96px;resize:vertical;font-family:ui-monospace,Menlo,monospace;font-size:13px}
 option{background:#141009;color:var(--ink)}
 .form-actions{margin:18px 0 8px;display:flex;gap:10px;align-items:center}
 .hint{font-size:12px;color:var(--faint);margin-top:5px;text-transform:none;letter-spacing:0;font-family:inherit;font-weight:400}
 .two{display:grid;grid-template-columns:1fr 1fr;gap:14px}
 @media (max-width:520px){.two{grid-template-columns:1fr}}
 .check{display:flex;align-items:center;gap:9px;font-size:14px;color:var(--ink);cursor:pointer;margin-top:16px}
 .check input{width:auto}
 .chain{font-size:12.5px;color:var(--muted);background:var(--line2);border:1px solid var(--line);
   border-radius:8px;padding:8px 11px;margin-top:8px;font-family:ui-monospace,Menlo,monospace}
 .chain b{font-weight:700}
 .chain .ok{color:var(--gold)} .chain .ko{color:var(--faint)}
 /* overlay générique (digest + éditeur) */
 .ov{position:fixed;inset:0;background:rgba(5,4,2,.74);display:none;z-index:20;backdrop-filter:blur(3px)}
 .ov.on{display:block}
 #ov .panel{position:absolute;inset:24px;background:#141009;border:1px solid var(--line);border-radius:14px;
   overflow:hidden;display:flex;flex-direction:column;box-shadow:0 0 40px rgba(230,184,74,.15)}
 #ov .bar{display:flex;justify-content:flex-end;padding:8px;border-bottom:1px solid var(--line)}
 #ov iframe{border:0;flex:1;width:100%;background:#fff}
 .sheet{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:min(560px,calc(100% - 32px));
   max-height:calc(100% - 48px);overflow:auto;background:#141009;border:1px solid var(--line);border-radius:14px;
   padding:22px 24px 20px;box-shadow:0 0 44px rgba(230,184,74,.16),0 20px 50px rgba(0,0,0,.6)}
 .sheet h3{margin:0 0 2px;font-size:1.15rem;text-transform:uppercase;letter-spacing:.08em}
 /* barre du haut : réglages (gauche) + statut système (droite), façon HUD */
 .topbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:24px}
 .gear{font:inherit;display:inline-flex;align-items:center;gap:7px;background:rgba(230,184,74,.05);
   border:1px solid var(--line);border-radius:8px;padding:8px 14px;cursor:pointer;color:var(--ink);
   font-size:13px;font-weight:600;letter-spacing:.04em}
 .gear:hover{border-color:var(--gold);box-shadow:0 0 10px rgba(230,184,74,.2)}
 .gear .dot{width:7px;height:7px;border-radius:50%;background:#e2342f;margin-left:2px;box-shadow:0 0 8px #e2342f}
 .hud-srv{display:flex;align-items:center;gap:8px;font-family:ui-monospace,Menlo,monospace;font-size:11px;
   letter-spacing:.2em;text-transform:uppercase;color:var(--gold)}
 .hud-srv .led{width:7px;height:7px;border-radius:50%;background:var(--gold);box-shadow:0 0 9px var(--gold)}
 .hud-srv .led.off{background:var(--faint);box-shadow:none}
</style></head><body>
<div class="wrap">
 <div class="topbar">
   <button class="gear" onclick="openSettings()" title="Réglages">⚙️ Réglages
     {% if not has_key %}<span class="dot" title="Aucune clé LLM"></span>{% endif %}</button>
   <div class="hud-srv"><span class="led {{'' if sched_on else 'off'}}"></span>srv&nbsp;007 · {{'scheduler online' if sched_on else 'scheduler standby'}}{% if auth_on %} · <a href="/logout" style="color:inherit;text-decoration:none">déconnexion</a>{% endif %}</div>
 </div>
 <header>
   <div class="mark"><img src="/img/cardinal-icon.png" alt="Cardinal"></div>
   <div><h1>Cardinal</h1><div class="sub">Système Cardinal · Veille autonome</div></div>
 </header>
 <p class="lead">Tes sujets tournent quand tu le décides. Ajoute, modifie, lance, lis.</p>

 {% if not has_key %}
 <div class="banner">⚠️ Aucune clé LLM détectée. Ouvre les
 <b><a href="#" onclick="return openSettings()" style="color:inherit">Réglages</a></b> (en haut à gauche)
 et colle une clé gratuite (Gemini conseillé) pour pouvoir lancer les veilles.</div>
 {% endif %}

 <section class="recap">
   <div class="recap-nav">
     <button class="recap-arrow" id="recap-prev" onclick="recapDay(recapOffset+1)">◄ Précédent</button>
     <div class="recap-day" id="recap-label">…</div>
     <button class="recap-arrow" id="recap-next" onclick="recapDay(recapOffset-1)">Suivant ►</button>
   </div>
   <div id="recap-body" class="recap-body"></div>
 </section>

 <div class="section-h"><h2>Sujets</h2><span class="line"></span></div>
 <div class="grid">
  {% for c in cards %}
  <div class="card" data-id="{{c.t.id}}" data-name="{{c.t.name}}" data-source="{{c.t.source}}"
       data-freq="{{c.t.frequency_hours}}" data-sev="{{c.t.min_severity}}">
   <span class="unread" id="unread-{{c.t.id}}" title="News non lues"
         style="{{'' if c.unread else 'display:none'}}">{{c.unread}}</span>
   <span class="badge b-{{c.t.source}}">{{c.label}}</span>
   <h3>{{c.t.name}}</h3>
   <div class="meta">{{c.summary}} · toutes les {{c.t.frequency_hours}} h</div>
   <div class="auto">
    {% if c.next_ts %}<span class="pill on" data-next="{{c.next_ts}}">◉ auto · <span class="cd">…</span></span>
    {% elif c.enabled %}<span class="pill">auto en pause (global)</span>
    {% else %}<span class="pill">⏸ en pause</span>{% endif %}
   </div>
   <div class="status" id="st-{{c.t.id}}">
     {% if c.feed_n %}{{c.feed_n}} au feed{% if c.last_at %} · maj {{c.last_at}}{% endif %}{% endif %}
   </div>
   <textarea class="cfg-store" hidden>{{c.config_text}}</textarea>
   <div class="row">
    <button class="btn-go" onclick="runTopic('{{c.t.id}}')">Lancer</button>
    <a class="btn view-last" href="#" onclick="return openDigest('/feed/{{c.t.id}}')"
       style="{{'' if c.feed_n else 'display:none'}}" id="last-{{c.t.id}}">Voir le feed</a>
    <form method="post" action="/topics/{{c.t.id}}/toggle">
     <button class="btn icon-btn" type="submit" title="{{'Mettre en pause' if c.enabled else 'Activer la veille auto'}}">{{'⏸' if c.enabled else '▶'}}</button>
    </form>
    <button class="btn icon-btn" title="Modifier" onclick="editTopic('{{c.t.id}}')">✎</button>
    <form method="post" action="/topics/{{c.t.id}}/delete" class="push"
          onsubmit="return confirm('Supprimer ce sujet ?')">
     <button class="btn-del icon-btn" type="submit" title="Supprimer">🗑</button>
    </form>
   </div>
  </div>
  {% endfor %}
 </div>

 <details class="panel" style="margin-top:22px">
  <summary>➕ Ajouter un sujet</summary>
  <form method="post" action="/topics/add" id="add-form">
   <div class="two">
    <div><label>Identifiant (court, sans espace)</label><input name="id" placeholder="ex: rust" required></div>
    <div><label>Nom affiché</label><input name="name" placeholder="ex: Actu Rust" required></div>
   </div>
   <div class="two">
    <div><label>Type de source</label>
     <select name="source" class="src" onchange="applySrc(this.closest('form'))">
      <option value="news">Actu — je tape juste le sujet (auto, conseillé)</option>
      <option value="web">RSS — je fournis les flux (avancé)</option>
      <option value="kev">CVE exploitées — patch urgent (CISA KEV)</option>
      <option value="nvd">CVE produits (iOS, Windows…)</option>
      <option value="osv">CVE dépendances (paquets)</option>
     </select></div>
    <div><label>Fréquence (heures)</label><input name="frequency_hours" type="number" value="24" min="1"></div>
   </div>
   <label class="cfg-label">Un flux RSS par ligne</label>
   <textarea name="config" class="cfg" placeholder="https://exemple.com/feed"></textarea>
   <div class="hint cfg-hint">Astuce : souvent l'URL du site + /feed ou /rss.</div>
   <div class="sev-wrap" style="display:none">
    <label>Sévérité minimale (CVE produits)</label>
    <select name="min_severity">
     <option value="">toutes</option><option value="MEDIUM">MEDIUM+</option>
     <option value="HIGH">HIGH+</option><option value="CRITICAL">CRITICAL</option>
    </select>
   </div>
   <div class="form-actions"><button class="btn-go" type="submit">Créer le sujet</button></div>
  </form>
 </details>
</div>

<!-- Overlay Réglages (compte / clés) -->
<div id="settings" class="ov {% if not has_key %}on{% endif %}"><div class="sheet">
  <h3>⚙️ Réglages</h3>
  <form method="post" action="/settings">
   <label>Fournisseur principal</label>
   <select name="provider">
    <option value="gemini" {{'selected' if provider=='gemini'}}>Gemini (gratuit, conseillé)</option>
    <option value="groq" {{'selected' if provider=='groq'}}>Groq (rapide)</option>
    <option value="openrouter" {{'selected' if provider=='openrouter'}}>OpenRouter</option>
   </select>
   <div class="hint">Renseigne plusieurs clés : si un fournisseur est à court de quota,
    Cardinal bascule tout seul sur le suivant.</div>
   <div class="chain">Chaîne d'essai active :
    <b class="{{'ok' if keys.gemini else 'ko'}}">Gemini {{'✓' if keys.gemini else '—'}}</b> →
    <b class="{{'ok' if keys.groq else 'ko'}}">Groq {{'✓' if keys.groq else '—'}}</b> →
    <b class="{{'ok' if keys.openrouter else 'ko'}}">OpenRouter {{'✓' if keys.openrouter else '—'}}</b></div>

   <div class="two">
    <div><label>Clé Gemini {% if keys.gemini %}<span class="hint">(enregistrée ✓)</span>{% endif %}</label>
     <input name="gemini_key" type="password" placeholder="{{'laisse vide pour garder' if keys.gemini else 'aistudio.google.com/apikey'}}" autocomplete="off"></div>
    <div><label>Clé Groq {% if keys.groq %}<span class="hint">(enregistrée ✓)</span>{% endif %}</label>
     <input name="groq_key" type="password" placeholder="{{'laisse vide pour garder' if keys.groq else 'console.groq.com/keys'}}" autocomplete="off"></div>
   </div>
   <label>Clé OpenRouter {% if keys.openrouter %}<span class="hint">(enregistrée ✓)</span>{% endif %}</label>
   <input name="openrouter_key" type="password" placeholder="{{'laisse vide pour garder' if keys.openrouter else 'optionnel — openrouter.ai/keys'}}" autocomplete="off">
   <label>Modèle OpenRouter <span class="hint">(les modèles gratuits changent ; vide = défaut)</span></label>
   <input name="openrouter_model" value="{{or_model}}" placeholder="{{or_model_default}}" autocomplete="off">
   <div class="hint">Modèles gratuits à jour : openrouter.ai/models?max_price=0</div>

   <label>Clé NVD (optionnelle — accélère les CVE produits) {% if has_nvd %}<span class="hint">(enregistrée ✓)</span>{% endif %}</label>
   <input name="nvd_key" type="password" placeholder="optionnel" autocomplete="off">

   <label class="check"><input type="checkbox" name="scheduler" {{'checked' if sched_on}}>
     Veille automatique : lancer chaque sujet tout seul selon sa fréquence</label>
   <div class="hint">Interrupteur global. Chaque sujet a aussi son bouton ▶/⏸.
     Cardinal doit rester ouvert (le terminal aussi) pour que ça tourne.</div>

   <label style="margin-top:14px">Notifications push (ntfy.sh)</label>
   <div class="hint">Alerte sur ton téléphone quand une <b>CVE priorité haute</b> tombe, même site fermé.
     Installe l'app <b>ntfy</b>, abonne-toi à un « topic » au nom secret, et remets ce même topic ici.</div>
   <div class="two">
    <div><label>Topic ntfy {% if ntfy_topic %}<span class="hint">(actif ✓)</span>{% endif %}</label>
     <input name="ntfy_topic" value="{{ntfy_topic}}" placeholder="ex: cardinal-a7f3k9-secret" autocomplete="off"></div>
    <div><label>Serveur <span class="hint">(vide = ntfy.sh)</span></label>
     <input name="ntfy_server" value="{{ntfy_server}}" placeholder="https://ntfy.sh" autocomplete="off"></div>
   </div>
   <div class="form-actions" style="margin-top:8px">
     <button type="button" class="btn" onclick="testNotify()">🔔 Tester la notif</button>
     <span id="notify-out" class="hint"></span>
   </div>

   <div class="form-actions">
     <button class="btn-go" type="submit">Enregistrer</button>
     <button type="button" class="btn" onclick="closeSettings()">Fermer</button>
   </div>
  </form>
  {% if auth_on %}
  <label style="margin-top:16px">Mise à jour du serveur</label>
  <div class="hint">Récupère la dernière version (<code>git pull</code>) puis redémarre Cardinal —
    redémarrage seulement s'il y a du nouveau.</div>
  <div class="form-actions">
    <button type="button" class="btn" id="update-btn" onclick="updateApp()">⟳ Mettre à jour</button>
  </div>
  <pre id="update-out" style="white-space:pre-wrap;font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--muted);margin:8px 0 0;max-height:160px;overflow:auto;display:none"></pre>
  {% endif %}
</div></div>

<!-- Overlay lecture du digest -->
<div id="ov" class="ov"><div class="panel"><div class="bar"><button onclick="closeDigest()">Fermer ✕</button></div>
 <iframe id="ovf"></iframe></div></div>

<!-- Overlay édition d'un sujet -->
<div id="edit" class="ov"><div class="sheet">
  <h3>Modifier le sujet</h3>
  <div class="hint" id="edit-id-label"></div>
  <form method="post" id="edit-form">
   <label>Nom affiché</label><input name="name" id="edit-name" required>
   <div class="two">
    <div><label>Type de source</label>
     <select name="source" class="src" onchange="applySrc(this.closest('form'))">
      <option value="news">Actu — je tape juste le sujet (auto, conseillé)</option>
      <option value="web">RSS — je fournis les flux (avancé)</option>
      <option value="kev">CVE exploitées — patch urgent (CISA KEV)</option>
      <option value="nvd">CVE produits (iOS, Windows…)</option>
      <option value="osv">CVE dépendances (paquets)</option>
     </select></div>
    <div><label>Fréquence (heures)</label><input name="frequency_hours" id="edit-freq" type="number" min="1"></div>
   </div>
   <label class="cfg-label">Un flux RSS par ligne</label>
   <textarea name="config" class="cfg"></textarea>
   <div class="hint cfg-hint"></div>
   <div class="sev-wrap" style="display:none">
    <label>Sévérité minimale (CVE produits)</label>
    <select name="min_severity" id="edit-sev">
     <option value="">toutes</option><option value="MEDIUM">MEDIUM+</option>
     <option value="HIGH">HIGH+</option><option value="CRITICAL">CRITICAL</option>
    </select>
   </div>
   <div class="form-actions">
     <button class="btn-go" type="submit">Enregistrer</button>
     <button type="button" class="btn" onclick="closeEdit()">Annuler</button>
   </div>
  </form>
</div></div>

<script>
const SRC={
 news:{label:'Un sujet par ligne — Cardinal cherche l\'actu tout seul',hint:'Aucun lien à fournir. Ex : réalité augmentée · actualité intelligence artificielle · Freebox Ultra.',ph:'réalité augmentée\nactualité jeux vidéo',sev:false},
 web:{label:'Un flux RSS par ligne',hint:"Souvent l'URL du site + /feed ou /rss.",ph:'https://exemple.com/feed',sev:false},
 nvd:{label:'Un produit par ligne (mot-clé)',hint:'Ex: Apple iOS · Windows 11 · WireGuard. Préfixe le fabricant = moins de bruit.',ph:'Apple iOS\nWindows 11\nWireGuard',sev:true},
 osv:{label:'Un paquet par ligne : nom écosystème',hint:'Écosystèmes : PyPI, npm, Go, crates.io, Maven, NuGet…',ph:'requests PyPI\nexpress npm',sev:false},
 kev:{label:'Un produit par ligne (mot-clé)',hint:'CVE activement exploitées (CISA KEV), temps quasi réel, sans LLM. Préfixe l\'éditeur pour éviter le bruit (Apple iOS ≠ Cisco IOS). Nom AMONT, pas la distro (Ubuntu/CentOS…).',ph:'Google Chrome\nMozilla Firefox\nApple iOS\nApple macOS\nMicrosoft Windows\nOpenSSH\nLinux Kernel',sev:false},
};
// Adapte libellés/placeholder/sévérité au type de source, pour un formulaire donné.
function applySrc(form){
 const d=SRC[form.querySelector('.src').value]||SRC.web;
 form.querySelector('.cfg-label').textContent=d.label;
 form.querySelector('.cfg-hint').textContent=d.hint;
 form.querySelector('.cfg').placeholder=d.ph;
 form.querySelector('.sev-wrap').style.display=d.sev?'block':'none';
}
applySrc(document.getElementById('add-form'));

// Pré-remplit et ouvre l'éditeur à partir des données de la carte.
function editTopic(id){
 const card=document.querySelector('.card[data-id="'+id+'"]');
 const f=document.getElementById('edit-form');
 f.action='/topics/'+id+'/edit';
 document.getElementById('edit-id-label').textContent='Identifiant : '+id+' (non modifiable)';
 document.getElementById('edit-name').value=card.dataset.name;
 document.getElementById('edit-freq').value=card.dataset.freq;
 f.querySelector('.src').value=card.dataset.source;
 f.querySelector('.cfg').value=card.querySelector('.cfg-store').value;
 document.getElementById('edit-sev').value=card.dataset.sev||'';
 applySrc(f);
 document.getElementById('edit').classList.add('on');
}
function closeEdit(){document.getElementById('edit').classList.remove('on');}
document.getElementById('edit').addEventListener('click',e=>{if(e.target.id==='edit')closeEdit();});

// Réglages (modale)
function openSettings(){document.getElementById('settings').classList.add('on');return false;}
function closeSettings(){document.getElementById('settings').classList.remove('on');}
async function updateApp(){
 const btn=document.getElementById('update-btn'), out=document.getElementById('update-out');
 btn.disabled=true; out.style.display='block'; out.textContent='Mise à jour…';
 try{
  const d=await (await fetch('/api/update',{method:'POST'})).json();
  out.textContent=d.output||'(pas de sortie)';
  if(d.restarting){
   out.textContent+='\n\nNouveautés récupérées — redémarrage… la page va se recharger.';
   setTimeout(()=>location.reload(), 5000);
  }else{
   if(d.ok) out.textContent+='\n\nDéjà à jour — pas de redémarrage.';
   btn.disabled=false;
  }
 }catch(e){ out.textContent='Erreur : '+e; btn.disabled=false; }
}
async function testNotify(){
 const out=document.getElementById('notify-out');
 const topic=document.querySelector('#settings [name=ntfy_topic]').value.trim();
 const server=document.querySelector('#settings [name=ntfy_server]').value.trim();
 if(!topic){ out.textContent='Renseigne un topic d\'abord.'; return; }
 out.textContent='Envoi…';
 try{
  const d=await (await fetch('/api/notify-test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({topic,server})})).json();
  out.textContent=d.ok?'✅ Envoyée — regarde ton téléphone.':('❌ '+(d.msg||'échec'));
 }catch(e){ out.textContent='❌ '+e; }
}
document.getElementById('settings').addEventListener('click',e=>{if(e.target.id==='settings')closeSettings();});

document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeEdit();closeDigest();closeSettings();}});

// Compte à rebours live du prochain run automatique de chaque sujet.
function fmtCd(s){
 if(s<=0) return 'imminent';
 const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60),ss=Math.floor(s%60);
 if(d>0) return d+'j '+h+'h';
 if(h>0) return h+'h '+String(m).padStart(2,'0')+'m';
 if(m>0) return m+'m '+String(ss).padStart(2,'0')+'s';
 return ss+'s';
}
function tickCd(){
 const now=Date.now()/1000;
 document.querySelectorAll('.pill.on[data-next]').forEach(p=>{
  const cd=p.querySelector('.cd'); if(cd) cd.textContent=fmtCd(Math.floor(+p.dataset.next-now));
 });
}
setInterval(tickCd,1000); tickCd();

// Récap du jour : news groupées par sujet, navigables jour par jour.
let recapOffset=0;
function escH(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
async function recapDay(off){
 off=Math.max(0,off);
 let d; try{ d=await (await fetch('/api/recap/'+off)).json(); }catch(e){ return; }
 recapOffset=d.offset;
 document.getElementById('recap-label').textContent=d.label;
 document.getElementById('recap-prev').disabled=!d.can_prev;
 document.getElementById('recap-next').disabled=!d.can_next;
 const body=document.getElementById('recap-body');
 if(!d.groups.length){ body.innerHTML='<div class="recap-empty">Aucune news ce jour-là.</div>'; return; }
 body.innerHTML=d.groups.map(g=>`
  <div class="recap-group">
   <div class="recap-gh"><span class="badge b-${g.source}">${escH(g.label)}</span> ${escH(g.name)}</div>
   <ul>${g.items.map(it=>{const u=(it.sources[0]||{}).url||'#';return `<li><a href="${escH(u)}" target="_blank" rel="noopener">${escH(it.title)}</a>${it.fix?`<span class="recap-fix">${escH(it.fix)}</span>`:''}${it.context?`<div class="recap-ctx">${escH(it.context)}</div>`:''}</li>`;}).join('')}</ul>
  </div>`).join('');
}
recapDay(0);

// Rafraîchit le dashboard tout seul (ronds non-lus, compteur, prochain run)
// quand le scheduler tourne en arrière-plan — sans recharger la page.
async function refreshDash(){
 try{
  const d=await (await fetch('/api/dashboard')).json();
  for(const id in d){
   const info=d[id];
   const b=document.getElementById('unread-'+id);
   if(b){ if(info.unread>0){b.textContent=info.unread;b.style.display='';}else{b.style.display='none';} }
   const voir=document.getElementById('last-'+id);
   if(voir && info.feed_n>0) voir.style.display='';
   const card=document.querySelector('.card[data-id="'+id+'"]');
   const pill=card && card.querySelector('.pill.on[data-next]');
   if(pill && info.next_ts) pill.dataset.next=info.next_ts;
  }
  tickCd();
  if(recapOffset===0) recapDay(0);   // garde le récap du jour à jour
 }catch(e){}
}
setInterval(refreshDash,20000);

function openDigest(url){
 const m=url.match(/\/feed\/([^/?#]+)/);   // ouvrir un feed = marquer lu -> on retire le rond
 if(m){const b=document.getElementById('unread-'+m[1]); if(b) b.style.display='none';}
 document.getElementById('ovf').src=url;document.getElementById('ov').classList.add('on');return false;}
function closeDigest(){document.getElementById('ov').classList.remove('on');document.getElementById('ovf').src='about:blank';}
async function runTopic(id){
 const st=document.getElementById('st-'+id);
 const r=await fetch('/api/run/'+id,{method:'POST'}); const {job}=await r.json();
 st.innerHTML='<span class="spin"></span>Recherche en cours…';
 let tries=0;
 const poll=setInterval(async()=>{
  if(++tries>160){clearInterval(poll);   // garde-fou : ~4 min max
   st.textContent='⏳ Toujours en cours — regarde le terminal, ou réessaie plus tard.';return;}
  const s=await (await fetch('/api/status/'+job)).json();
  if(s.status==='done'){clearInterval(poll);
   st.textContent=(s.added>0?s.added+' nouveau(x) · ':'rien de neuf · ')+s.total+' au feed';
   document.getElementById('last-'+id).style.display='';
   openDigest('/feed/'+id);
  } else if(s.status==='error'){clearInterval(poll);
   st.innerHTML='❌ '+s.error;}
 },1500);
}
</script>
</body></html>"""


# Sous gunicorn (production), __main__ n'est pas exécuté : on démarre le
# scheduler via CARDINAL_SCHEDULER=1 (à mettre avec gunicorn --workers 1 pour
# n'avoir qu'UN scheduler). En local (python app.py) c'est le bloc ci-dessous.
if os.getenv("CARDINAL_SCHEDULER") == "1":
    start_scheduler()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    if os.getenv("CARDINAL_SCHEDULER") != "1":
        start_scheduler()  # veille automatique en tâche de fond
    print(f"Cardinal ▸ http://127.0.0.1:{port}  (scheduler actif)")
    app.run(host="127.0.0.1", port=port, debug=False)
