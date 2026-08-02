"""
Cardinal — starter : 1 sujet -> 1 run -> 1 digest HTML.

Usage :
    python agent.py list                # liste les sujets
    python agent.py run <topic_id>      # lance un run et génère le HTML

Exemple : python agent.py run ar
"""

import os
import sys
import datetime as dt

from dotenv import load_dotenv

import research as research_mod
import render as render_mod
import store

load_dotenv()  # permet de garder la clé dans .env si tu préfères le terminal


def cmd_list():
    store.init_db()
    print("Sujets disponibles :")
    for t in store.list_topics():
        counts = {"web": f"{len(t.feeds)} flux RSS",
                  "osv": f"{len(t.packages)} paquet(s)",
                  "nvd": f"{len(t.keywords)} produit(s)"}
        extra = counts.get(t.source, "")
        print(f"  - {t.id:<10} [{t.source}] {t.name} — {extra}")


def cmd_run(topic_id: str):
    store.init_db()
    topic = store.get_topic(topic_id)

    print(f"→ Run '{topic.name}' (source={topic.source}, fenêtre={topic.frequency_hours}h)…")
    digest = research_mod.research(topic)

    # Le feed est continu : on empile les nouveaux items (la dédup des sources
    # déjà vues est faite en amont dans research), puis on mémorise les clés vues.
    keys = digest.pop("_item_keys", None)
    items = digest.get("items", [])
    added = store.add_feed_items(topic.id, items) if items else 0
    if keys:
        store.mark_seen(topic.id, keys)
    store.purge_feed()  # fenêtre glissante : retire ce qui a plus d'un mois

    # On écrit aussi un instantané HTML de ce run (pratique en CLI hors dashboard).
    html = render_mod.render_html(digest, topic)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join("digests", f"{topic.id}_{stamp}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)

    store.save_run(topic.id, path, digest)
    print(f"✓ {added} nouvel(le)(s) entrée(s) ajoutée(s) au feed "
          f"({store.feed_count(topic.id)} au total sur 30 jours).")
    print(f"  Instantané de ce run : {path}")


def main():
    args = sys.argv[1:]
    if not args or args[0] == "list":
        cmd_list()
    elif args[0] == "run" and len(args) == 2:
        cmd_run(args[1])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
