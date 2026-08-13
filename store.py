"""
Persistance SQLite : sujets, runs (digests archivés), clés déjà vues, réglages.
Les sujets sont désormais des DONNÉES (plus du code) : on peut les créer/supprimer
depuis le dashboard.
"""

import json
import hashlib
import sqlite3
import datetime as dt

from topics import Topic, DEFAULT_TOPICS

DB_PATH = "cardinal.db"

# Fenêtre du feed : on garde en permanence ~1 mois d'items (glissant).
FEED_DAYS = 30


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=5000")  # patience si une écriture concurrente verrouille
    return c


def init_db():
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS topics (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                source          TEXT NOT NULL,
                frequency_hours INTEGER NOT NULL,
                config          TEXT NOT NULL,
                enabled         INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS runs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id   TEXT NOT NULL,
                created_at TEXT NOT NULL,
                html_path  TEXT NOT NULL,
                digest     TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS seen (
                topic_id  TEXT NOT NULL,
                item_key  TEXT NOT NULL,
                seen_at   TEXT NOT NULL,
                PRIMARY KEY (topic_id, item_key)
            );
            -- Feed continu : chaque item d'un digest y est empilé (dédupliqué),
            -- puis les items de plus d'un mois sont purgés (fenêtre glissante).
            CREATE TABLE IF NOT EXISTS feed_items (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id   TEXT NOT NULL,
                item_key   TEXT NOT NULL,
                title      TEXT,
                body       TEXT,
                importance TEXT,
                fix        TEXT,
                sources    TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (topic_id, item_key)
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            -- Comptes (multi-utilisateurs). Le premier créé est admin.
            CREATE TABLE IF NOT EXISTS users (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                is_admin   INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            -- Passkeys WebAuthn : 1 credential = 1 ligne (un user peut en avoir plusieurs).
            CREATE TABLE IF NOT EXISTS credentials (
                cred_id    TEXT PRIMARY KEY,        -- base64url de l'ID de credential
                user_id    TEXT NOT NULL,
                public_key BLOB NOT NULL,
                sign_count INTEGER NOT NULL DEFAULT 0,
                transports TEXT,
                created_at TEXT NOT NULL
            );
            -- Invitations : jeton à usage unique pour créer un compte non-admin.
            CREATE TABLE IF NOT EXISTS invites (
                token      TEXT PRIMARY KEY,
                created_by TEXT NOT NULL,
                label      TEXT,
                created_at TEXT NOT NULL,
                used_by    TEXT,
                used_at    TEXT,
                expires_at TEXT
            );
            """
        )
        # Migrations douces des bases déjà créées.
        fcols = [r["name"] for r in c.execute("PRAGMA table_info(feed_items)")]
        if "fix" not in fcols:
            c.execute("ALTER TABLE feed_items ADD COLUMN fix TEXT")
        tcols = [r["name"] for r in c.execute("PRAGMA table_info(topics)")]
        if "enabled" not in tcols:
            c.execute("ALTER TABLE topics ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
        # Multi-utilisateurs : propriétaire du sujet. NULL = données « legacy »
        # (créées avant l'introduction des comptes) — réattribuées au 1er admin.
        if "owner_id" not in tcols:
            c.execute("ALTER TABLE topics ADD COLUMN owner_id TEXT")
    seed_topics()


# --- Sujets ---------------------------------------------------------------
def _config_of(t: Topic) -> str:
    return json.dumps(
        {
            "feeds": t.feeds,
            "packages": t.packages,
            "keywords": t.keywords,
            "min_severity": t.min_severity,
        },
        ensure_ascii=False,
    )


def _row_to_topic(row) -> Topic:
    cfg = json.loads(row["config"])
    keys = row.keys()
    return Topic(
        id=row["id"],
        name=row["name"],
        source=row["source"],
        frequency_hours=row["frequency_hours"],
        feeds=cfg.get("feeds", []),
        packages=cfg.get("packages", []),
        keywords=cfg.get("keywords", []),
        min_severity=cfg.get("min_severity", ""),
        owner_id=(row["owner_id"] if "owner_id" in keys else None),
    )


def seed_topics():
    """Insère les sujets par défaut si la table est vide (premier lancement)."""
    with _conn() as c:
        n = c.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
        if n:
            return
        for t in DEFAULT_TOPICS.values():
            c.execute(
                "INSERT INTO topics (id, name, source, frequency_hours, config) VALUES (?,?,?,?,?)",
                (t.id, t.name, t.source, t.frequency_hours, _config_of(t)),
            )


def list_topics(owner_id: str = None) -> list:
    """Tous les sujets, ou seulement ceux d'un propriétaire si `owner_id` est fourni.
    Le scheduler appelle sans argument (il itère tous les espaces)."""
    with _conn() as c:
        if owner_id is None:
            rows = c.execute("SELECT * FROM topics ORDER BY name").fetchall()
        else:
            rows = c.execute("SELECT * FROM topics WHERE owner_id=? ORDER BY name",
                             (owner_id,)).fetchall()
    return [_row_to_topic(r) for r in rows]


def get_topic(topic_id: str, owner_id: str = None) -> Topic:
    """Sujet par id. Si `owner_id` est fourni, refuse (KeyError) un sujet qui ne lui
    appartient pas → isolation appliquée côté base."""
    with _conn() as c:
        row = c.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
    if not row:
        raise KeyError(f"Sujet inconnu : {topic_id}")
    t = _row_to_topic(row)
    if owner_id is not None and t.owner_id != owner_id:
        raise KeyError(f"Sujet inconnu : {topic_id}")
    return t


def topic_id_available(topic_id: str) -> bool:
    """L'id de sujet est un PK global (unique dans toute l'instance)."""
    with _conn() as c:
        return c.execute("SELECT 1 FROM topics WHERE id=?", (topic_id,)).fetchone() is None


def upsert_topic(t: Topic):
    with _conn() as c:
        c.execute(
            "INSERT INTO topics (id, name, source, frequency_hours, config, owner_id) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, source=excluded.source, "
            "frequency_hours=excluded.frequency_hours, config=excluded.config",
            (t.id, t.name, t.source, t.frequency_hours, _config_of(t), t.owner_id),
        )


def delete_topic(topic_id: str):
    with _conn() as c:
        c.execute("DELETE FROM topics WHERE id=?", (topic_id,))
        c.execute("DELETE FROM runs WHERE topic_id=?", (topic_id,))
        c.execute("DELETE FROM feed_items WHERE topic_id=?", (topic_id,))
        c.execute("DELETE FROM seen WHERE topic_id=?", (topic_id,))


def is_enabled(topic_id: str) -> bool:
    """Veille automatique active pour ce sujet ? (scheduler)"""
    with _conn() as c:
        row = c.execute("SELECT enabled FROM topics WHERE id=?", (topic_id,)).fetchone()
    return bool(row["enabled"]) if row else False


def set_enabled(topic_id: str, enabled: bool):
    with _conn() as c:
        c.execute("UPDATE topics SET enabled=? WHERE id=?", (1 if enabled else 0, topic_id))


# --- Runs -----------------------------------------------------------------
def save_run(topic_id: str, html_path: str, digest: dict):
    with _conn() as c:
        c.execute(
            "INSERT INTO runs (topic_id, created_at, html_path, digest) VALUES (?,?,?,?)",
            (topic_id, dt.datetime.now(dt.timezone.utc).isoformat(), html_path,
             json.dumps(digest, ensure_ascii=False)),
        )


def latest_run(topic_id: str):
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM runs WHERE topic_id=? ORDER BY id DESC LIMIT 1", (topic_id,)
        ).fetchone()
    return dict(row) if row else None


# --- Feed continu ---------------------------------------------------------
def _feed_key(item: dict) -> str:
    """Clé de dédup d'un item dans le feed. On se base d'abord sur les URLs de
    sources (stables : une CVE garde ses mêmes liens d'un run à l'autre, même si
    le LLM reformule le titre). À défaut d'URL, on retombe sur le titre."""
    urls = sorted(s.get("url", "") for s in item.get("sources", []) if s.get("url"))
    base = "u|" + "|".join(urls) if urls else "t|" + (item.get("title", "") or "").strip().lower()
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def add_feed_items(topic_id: str, items: list) -> list:
    """Empile les items d'un digest dans le feed. Renvoie la LISTE des items
    RÉELLEMENT ajoutés (les doublons sont ignorés) — utile pour notifier sur les
    seules nouveautés. `len(...)` donne le compte."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    added = []
    with _conn() as c:
        for it in items:
            cur = c.execute(
                "INSERT OR IGNORE INTO feed_items "
                "(topic_id, item_key, title, body, importance, fix, sources, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (topic_id, _feed_key(it), it.get("title", ""), it.get("body", ""),
                 it.get("importance", "low"), it.get("fix", ""),
                 json.dumps(it.get("sources", []), ensure_ascii=False), now),
            )
            if cur.rowcount:
                added.append(it)
    return added


def _feed_cutoff(days: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()


def get_feed(topic_id: str, days: int = FEED_DAYS) -> list:
    """Items du feed d'un sujet sur la fenêtre (plus récents d'abord)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM feed_items WHERE topic_id=? AND created_at>=? ORDER BY id DESC",
            (topic_id, _feed_cutoff(days)),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["sources"] = json.loads(d["sources"] or "[]")
        out.append(d)
    return out


def feed_items_between(start_iso: str, end_iso: str, owner_id: str = None) -> list:
    """Items du feed créés dans [start, end[, récents d'abord. Sert au récap du jour.
    Si `owner_id` est fourni, ne renvoie que les items des sujets de ce propriétaire
    (jointure sur topics) → chaque user ne voit que son récap."""
    with _conn() as c:
        if owner_id is None:
            rows = c.execute(
                "SELECT * FROM feed_items WHERE created_at>=? AND created_at<? ORDER BY id DESC",
                (start_iso, end_iso),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT f.* FROM feed_items f JOIN topics t ON t.id=f.topic_id "
                "WHERE t.owner_id=? AND f.created_at>=? AND f.created_at<? ORDER BY f.id DESC",
                (owner_id, start_iso, end_iso),
            ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["sources"] = json.loads(d["sources"] or "[]")
        out.append(d)
    return out


def feed_count(topic_id: str, days: int = FEED_DAYS) -> int:
    with _conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM feed_items WHERE topic_id=? AND created_at>=?",
            (topic_id, _feed_cutoff(days)),
        ).fetchone()[0]


# --- Lu / non lu (notifications) ------------------------------------------
def mark_read(topic_id: str):
    """Marque le feed d'un sujet comme lu : on retient le plus grand id vu."""
    with _conn() as c:
        top = c.execute(
            "SELECT MAX(id) m FROM feed_items WHERE topic_id=?", (topic_id,)
        ).fetchone()["m"]
    set_setting(f"READ_ID:{topic_id}", str(top or 0))


def unread_count(topic_id: str, days: int = FEED_DAYS) -> int:
    """Nombre d'items du feed (fenêtre) plus récents que le dernier lu."""
    read_id = int(get_setting(f"READ_ID:{topic_id}", "0") or "0")
    with _conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM feed_items WHERE topic_id=? AND id>? AND created_at>=?",
            (topic_id, read_id, _feed_cutoff(days)),
        ).fetchone()[0]


def purge_feed(days: int = FEED_DAYS):
    """Retire les items de plus d'un mois — et EUX SEULS (fenêtre glissante).
    La table `seen` n'est pas purgée : un item sorti du feed ne « ressuscite » pas."""
    with _conn() as c:
        c.execute("DELETE FROM feed_items WHERE created_at < ?", (_feed_cutoff(days),))


# --- Déduplication --------------------------------------------------------
def already_seen(topic_id: str, keys: list) -> set:
    if not keys:
        return set()
    with _conn() as c:
        rows = c.execute(
            f"SELECT item_key FROM seen WHERE topic_id=? "
            f"AND item_key IN ({','.join('?' * len(keys))})",
            [topic_id, *keys],
        ).fetchall()
    return {r["item_key"] for r in rows}


def mark_seen(topic_id: str, keys: list):
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    with _conn() as c:
        c.executemany(
            "INSERT OR IGNORE INTO seen (topic_id, item_key, seen_at) VALUES (?,?,?)",
            [(topic_id, k, now) for k in keys],
        )


# --- Réglages (fournisseur LLM, clés) -------------------------------------
def set_setting(key: str, value: str):
    with _conn() as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_setting(key: str, default=None):
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row and row["value"] else default


# --- Réglages PAR UTILISATEUR (clés API perso) ----------------------------
# On réutilise la table `settings` en namespaçant la clé : « u:{uid}:{KEY} ».
# Les réglages d'instance (NTFY_*, SCHEDULER_ENABLED, SECRET_KEY) restent en clé nue.
def _ukey(uid: str, key: str) -> str:
    return f"u:{uid}:{key}"


def set_user_setting(uid: str, key: str, value: str):
    set_setting(_ukey(uid, key), value)


def get_user_setting(uid: str, key: str, default=None):
    return get_setting(_ukey(uid, key), default)


def delete_user_settings(uid: str):
    with _conn() as c:
        c.execute("DELETE FROM settings WHERE key LIKE ?", (f"u:{uid}:%",))


# --- Comptes / passkeys / invitations -------------------------------------
def count_users() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_user(uid: str, name: str, is_admin: bool) -> dict:
    with _conn() as c:
        c.execute(
            "INSERT INTO users (id, name, is_admin, created_at) VALUES (?,?,?,?)",
            (uid, name, 1 if is_admin else 0,
             dt.datetime.now(dt.timezone.utc).isoformat()),
        )
    return get_user(uid)


def get_user(uid: str):
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return dict(row) if row else None


def list_users() -> list:
    with _conn() as c:
        rows = c.execute("SELECT * FROM users ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def delete_user(uid: str):
    """Supprime un compte et TOUT son espace (sujets, feed, runs, clés, passkeys)."""
    with _conn() as c:
        tids = [r["id"] for r in c.execute("SELECT id FROM topics WHERE owner_id=?", (uid,))]
        for tid in tids:
            c.execute("DELETE FROM runs WHERE topic_id=?", (tid,))
            c.execute("DELETE FROM feed_items WHERE topic_id=?", (tid,))
            c.execute("DELETE FROM seen WHERE topic_id=?", (tid,))
        c.execute("DELETE FROM topics WHERE owner_id=?", (uid,))
        c.execute("DELETE FROM credentials WHERE user_id=?", (uid,))
        c.execute("DELETE FROM settings WHERE key LIKE ?", (f"u:{uid}:%",))
        c.execute("DELETE FROM users WHERE id=?", (uid,))


def add_credential(cred_id: str, user_id: str, public_key: bytes,
                   sign_count: int, transports: str = ""):
    with _conn() as c:
        c.execute(
            "INSERT INTO credentials (cred_id, user_id, public_key, sign_count, "
            "transports, created_at) VALUES (?,?,?,?,?,?)",
            (cred_id, user_id, public_key, sign_count, transports,
             dt.datetime.now(dt.timezone.utc).isoformat()),
        )


def get_credential(cred_id: str):
    with _conn() as c:
        row = c.execute("SELECT * FROM credentials WHERE cred_id=?", (cred_id,)).fetchone()
    return dict(row) if row else None


def update_sign_count(cred_id: str, sign_count: int):
    with _conn() as c:
        c.execute("UPDATE credentials SET sign_count=? WHERE cred_id=?",
                  (sign_count, cred_id))


def create_invite(token: str, created_by: str, label: str = "", expires_at: str = None):
    with _conn() as c:
        c.execute(
            "INSERT INTO invites (token, created_by, label, created_at, expires_at) "
            "VALUES (?,?,?,?,?)",
            (token, created_by, label,
             dt.datetime.now(dt.timezone.utc).isoformat(), expires_at),
        )


def get_invite(token: str):
    with _conn() as c:
        row = c.execute("SELECT * FROM invites WHERE token=?", (token,)).fetchone()
    return dict(row) if row else None


def invite_is_valid(token: str) -> bool:
    inv = get_invite(token)
    if not inv or inv["used_by"]:
        return False
    if inv["expires_at"] and inv["expires_at"] < dt.datetime.now(dt.timezone.utc).isoformat():
        return False
    return True


def consume_invite(token: str, used_by: str):
    with _conn() as c:
        c.execute("UPDATE invites SET used_by=?, used_at=? WHERE token=?",
                  (used_by, dt.datetime.now(dt.timezone.utc).isoformat(), token))


def list_invites(created_by: str = None) -> list:
    with _conn() as c:
        if created_by is None:
            rows = c.execute("SELECT * FROM invites ORDER BY created_at DESC").fetchall()
        else:
            rows = c.execute("SELECT * FROM invites WHERE created_by=? ORDER BY created_at DESC",
                             (created_by,)).fetchall()
    return [dict(r) for r in rows]


def claim_legacy(uid: str) -> int:
    """Réattribue au 1er admin toutes les données « legacy » (owner_id NULL) créées
    avant l'introduction des comptes. Renvoie le nombre de sujets réattribués."""
    with _conn() as c:
        cur = c.execute("UPDATE topics SET owner_id=? WHERE owner_id IS NULL", (uid,))
        return cur.rowcount
