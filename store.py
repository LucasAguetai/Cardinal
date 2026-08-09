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
            """
        )
        # Migrations douces des bases déjà créées.
        fcols = [r["name"] for r in c.execute("PRAGMA table_info(feed_items)")]
        if "fix" not in fcols:
            c.execute("ALTER TABLE feed_items ADD COLUMN fix TEXT")
        tcols = [r["name"] for r in c.execute("PRAGMA table_info(topics)")]
        if "enabled" not in tcols:
            c.execute("ALTER TABLE topics ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
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
    return Topic(
        id=row["id"],
        name=row["name"],
        source=row["source"],
        frequency_hours=row["frequency_hours"],
        feeds=cfg.get("feeds", []),
        packages=cfg.get("packages", []),
        keywords=cfg.get("keywords", []),
        min_severity=cfg.get("min_severity", ""),
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


def list_topics() -> list:
    with _conn() as c:
        rows = c.execute("SELECT * FROM topics ORDER BY name").fetchall()
    return [_row_to_topic(r) for r in rows]


def get_topic(topic_id: str) -> Topic:
    with _conn() as c:
        row = c.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
    if not row:
        raise KeyError(f"Sujet inconnu : {topic_id}")
    return _row_to_topic(row)


def upsert_topic(t: Topic):
    with _conn() as c:
        c.execute(
            "INSERT INTO topics (id, name, source, frequency_hours, config) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, source=excluded.source, "
            "frequency_hours=excluded.frequency_hours, config=excluded.config",
            (t.id, t.name, t.source, t.frequency_hours, _config_of(t)),
        )


def delete_topic(topic_id: str):
    with _conn() as c:
        c.execute("DELETE FROM topics WHERE id=?", (topic_id,))
        c.execute("DELETE FROM runs WHERE topic_id=?", (topic_id,))


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


def feed_items_between(start_iso: str, end_iso: str) -> list:
    """Tous les items du feed (tous sujets) créés dans [start, end[, récents d'abord.
    Sert au récap du jour."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM feed_items WHERE created_at>=? AND created_at<? ORDER BY id DESC",
            (start_iso, end_iso),
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
