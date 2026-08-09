"""
Rendu HTML du digest : un article autonome (CSS inline), agréable à lire.
Aucune dépendance externe — juste du templating string + html.escape.
"""

import os
import html
import datetime as dt

# Fuseau d'affichage explicite : sans ça, .astimezone() suit le fuseau du serveur
# (souvent UTC en prod) et l'heure paraît décalée. Configurable via CARDINAL_TZ.
try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(os.getenv("CARDINAL_TZ", "Europe/Paris"))
except Exception:
    _TZ = None  # repli : fuseau local du système


def _now() -> dt.datetime:
    return dt.datetime.now(_TZ) if _TZ else dt.datetime.now().astimezone()


def _to_local(d: dt.datetime) -> dt.datetime:
    return d.astimezone(_TZ) if _TZ else d.astimezone()


_BADGE = {
    "high": ("Priorité haute", "#b42318", "#fef3f2"),
    "medium": ("À suivre", "#b54708", "#fffaeb"),
    "low": ("Info", "#475467", "#f2f4f7"),
}


def _esc(s: str) -> str:
    return html.escape(s or "")


def _fix_html(item: dict) -> str:
    """Ligne « mettre à jour vers … » mise en évidence (CVE uniquement)."""
    fix = (item.get("fix") or "").strip()
    if not fix:
        return ""
    return f'<div class="fix"><span class="fix-tag">Mettre à jour</span>{_esc(fix)}</div>'


def _item_html(item: dict) -> str:
    label, color, bg = _BADGE.get(item.get("importance", "low"), _BADGE["low"])
    # body : on transforme les sauts de ligne en paragraphes
    paras = "".join(
        f"<p>{_esc(p.strip())}</p>"
        for p in (item.get("body", "") or "").split("\n")
        if p.strip()
    )
    sources = "".join(
        f'<a class="src" href="{_esc(s.get("url", "#"))}" target="_blank" '
        f'rel="noopener">{_esc(s.get("title") or s.get("url") or "source")}</a>'
        for s in item.get("sources", [])
    )
    src_block = f'<div class="sources">{sources}</div>' if sources else ""
    return f"""
    <article class="item">
      <span class="badge" style="color:{color};background:{bg}">{label}</span>
      <h2>{_esc(item.get("title", ""))}</h2>
      {paras}
      {_fix_html(item)}
      {src_block}
    </article>"""


def render_html(digest: dict, topic) -> str:
    now = _now()
    since = now - dt.timedelta(hours=topic.frequency_hours)
    window = f"{since.strftime('%d/%m')} → {now.strftime('%d/%m/%Y')}"

    items = digest.get("items", [])
    items_html = (
        "".join(_item_html(it) for it in items)
        if items
        else '<article class="item"><p class="empty">Rien de neuf sur cette période.</p></article>'
    )

    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(digest.get("headline", topic.name))}</title>
<link rel="icon" type="image/png" href="/img/favicon.png">
<style>
  :root {{ --ink:#1a1a1a; --muted:#667085; --line:#e4e7ec; --accent:#9a6a00; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:#f5f5f4; color:var(--ink);
         font-family: Georgia, 'Iowan Old Style', 'Times New Roman', serif;
         line-height:1.65; }}
  ::selection {{ background:rgba(230,184,74,.30); }}
  .wrap {{ max-width:680px; margin:0 auto; padding:56px 22px 96px; }}
  .kicker {{ font-family: ui-sans-serif, system-ui, sans-serif; font-weight:700;
            letter-spacing:.12em; text-transform:uppercase; font-size:12px;
            color:var(--accent); }}
  h1 {{ font-size:2.35rem; line-height:1.12; margin:.35em 0 .2em; }}
  .meta {{ font-family: ui-sans-serif, system-ui, sans-serif; font-size:13px;
          color:var(--muted); border-bottom:1px solid var(--line);
          padding-bottom:22px; margin-bottom:30px; }}
  .lede {{ font-size:1.2rem; color:#344054; margin:0 0 40px; }}
  .item {{ border-top:1px solid var(--line); padding:28px 0; }}
  .item:first-of-type {{ border-top:none; }}
  .item h2 {{ font-size:1.5rem; line-height:1.2; margin:.35em 0 .5em; }}
  .item p {{ margin:0 0 .9em; }}
  .empty {{ color:var(--muted); font-style:italic; }}
  .badge {{ display:inline-block; font-family: ui-sans-serif, system-ui, sans-serif;
           font-size:11px; font-weight:700; letter-spacing:.04em; text-transform:uppercase;
           padding:3px 9px; border-radius:999px; }}
  .fix {{ font-family: ui-sans-serif, system-ui, sans-serif; font-size:14px;
         font-weight:600; color:#065f46; background:#ecfdf3; border:1px solid #a7f3d0;
         border-radius:8px; padding:8px 12px; margin:6px 0 4px; display:flex;
         align-items:center; gap:8px; }}
  .fix-tag {{ font-size:10.5px; font-weight:700; letter-spacing:.04em; text-transform:uppercase;
             color:#fff; background:#059669; border-radius:999px; padding:2px 8px; }}
  .sources {{ margin-top:14px; display:flex; flex-wrap:wrap; gap:8px; }}
  .src {{ font-family: ui-sans-serif, system-ui, sans-serif; font-size:12.5px;
         color:var(--accent); text-decoration:none; border:1px solid var(--line);
         border-radius:6px; padding:4px 10px; background:#fff; }}
  .src:hover {{ border-color:var(--accent); }}
  footer {{ margin-top:56px; padding-top:20px; border-top:1px solid var(--line);
           font-family: ui-sans-serif, system-ui, sans-serif; font-size:12px;
           color:var(--muted); }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="kicker">Veille · {_esc(topic.name)}</div>
    <h1>{_esc(digest.get("headline", topic.name))}</h1>
    <div class="meta">Fenêtre {window} · généré le {now.strftime('%d/%m/%Y à %H:%M')} · {len(items)} sujet(s)</div>
    <p class="lede">{_esc(digest.get("summary", ""))}</p>
    {items_html}
    <footer>Digest produit automatiquement par Cardinal — source : {_esc(topic.source)}.</footer>
  </div>
</body>
</html>"""


# --------------------------------------------------------------------------
# Feed continu : tous les items accumulés (30 derniers jours), plus récents
# en haut, regroupés par jour. C'est la vue principale d'un sujet.
# --------------------------------------------------------------------------
_MONTHS = ["", "janvier", "février", "mars", "avril", "mai", "juin", "juillet",
           "août", "septembre", "octobre", "novembre", "décembre"]


def _parse_dt(iso: str) -> dt.datetime:
    try:
        return dt.datetime.fromisoformat(iso)
    except Exception:
        return dt.datetime.now(dt.timezone.utc)


def _feed_item_html(row: dict) -> str:
    label, color, bg = _BADGE.get(row.get("importance", "low"), _BADGE["low"])
    when = _to_local(_parse_dt(row.get("created_at", "")))
    paras = "".join(
        f"<p>{_esc(p.strip())}</p>"
        for p in (row.get("body", "") or "").split("\n")
        if p.strip()
    )
    sources = "".join(
        f'<a class="src" href="{_esc(s.get("url", "#"))}" target="_blank" '
        f'rel="noopener">{_esc(s.get("title") or s.get("url") or "source")}</a>'
        for s in row.get("sources", [])
    )
    src_block = f'<div class="sources">{sources}</div>' if sources else ""
    return f"""
    <article class="item">
      <div class="itemhead">
        <span class="badge" style="color:{color};background:{bg}">{label}</span>
        <span class="when">{when.strftime('%H:%M')}</span>
      </div>
      <h2>{_esc(row.get("title", ""))}</h2>
      {paras}
      {_fix_html(row)}
      {src_block}
    </article>"""


def render_feed(topic, items: list, days: int = 30, has_run: bool = True) -> str:
    """Page autonome : le feed continu d'un sujet, groupé par jour.
    `has_run` : le sujet a-t-il déjà été lancé au moins une fois ? (adapte le
    message quand le feed est vide : « lance une veille » vs « rien trouvé »)."""
    now = _now()

    blocks, current_day = [], None
    for row in items:
        d = _to_local(_parse_dt(row.get("created_at", "")))
        day_key = d.strftime("%Y-%m-%d")
        if day_key != current_day:
            current_day = day_key
            label = "Aujourd'hui" if d.date() == now.date() else \
                f"{d.day} {_MONTHS[d.month]} {d.year}"
            blocks.append(f'<div class="day">{label}</div>')
        blocks.append(_feed_item_html(row))

    if items:
        body = "".join(blocks)
    elif not has_run:
        body = ('<p class="empty">Le feed est encore vide. Lance une veille : '
                'les résultats s\'empileront ici et resteront visibles pendant un mois.</p>')
    elif topic.source == "kev":
        body = ('<p class="empty">✅ Aucune CVE activement exploitée sur tes produits — '
                'c\'est bon signe. Cardinal surveille en continu et affichera ici, en '
                'priorité, toute CVE de tes technos visée par une attaque connue.</p>')
    else:
        body = ('<p class="empty">Rien de neuf pour l\'instant. Cardinal continue de '
                'surveiller — les nouveautés apparaîtront ici automatiquement.</p>')

    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(topic.name)} — feed</title>
<link rel="icon" type="image/png" href="/img/favicon.png">
<style>
  :root {{ --ink:#1a1a1a; --muted:#667085; --line:#e4e7ec; --accent:#9a6a00; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:#f5f5f4; color:var(--ink);
         font-family: Georgia, 'Iowan Old Style', 'Times New Roman', serif;
         line-height:1.65; }}
  ::selection {{ background:rgba(230,184,74,.30); }}
  .wrap {{ max-width:680px; margin:0 auto; padding:48px 22px 96px; }}
  .kicker {{ font-family: ui-sans-serif, system-ui, sans-serif; font-weight:700;
            letter-spacing:.12em; text-transform:uppercase; font-size:12px;
            color:var(--accent); }}
  h1 {{ font-size:2.1rem; line-height:1.12; margin:.35em 0 .2em; }}
  .meta {{ font-family: ui-sans-serif, system-ui, sans-serif; font-size:13px;
          color:var(--muted); border-bottom:1px solid var(--line);
          padding-bottom:20px; margin-bottom:12px; }}
  .day {{ position:sticky; top:0; background:#f5f5f4;
         font-family: ui-sans-serif, system-ui, sans-serif; font-weight:700;
         font-size:13px; letter-spacing:.03em; text-transform:uppercase;
         color:var(--muted); padding:18px 0 8px; }}
  .item {{ border-top:1px solid var(--line); padding:22px 0; }}
  .itemhead {{ display:flex; align-items:center; gap:10px; }}
  .when {{ font-family: ui-sans-serif, system-ui, sans-serif; font-size:12px;
          color:var(--muted); }}
  .item h2 {{ font-size:1.4rem; line-height:1.22; margin:.4em 0 .5em; }}
  .item p {{ margin:0 0 .9em; }}
  .empty {{ color:var(--muted); font-style:italic; }}
  .badge {{ display:inline-block; font-family: ui-sans-serif, system-ui, sans-serif;
           font-size:11px; font-weight:700; letter-spacing:.04em; text-transform:uppercase;
           padding:3px 9px; border-radius:999px; }}
  .fix {{ font-family: ui-sans-serif, system-ui, sans-serif; font-size:14px;
         font-weight:600; color:#065f46; background:#ecfdf3; border:1px solid #a7f3d0;
         border-radius:8px; padding:8px 12px; margin:6px 0 4px; display:flex;
         align-items:center; gap:8px; }}
  .fix-tag {{ font-size:10.5px; font-weight:700; letter-spacing:.04em; text-transform:uppercase;
             color:#fff; background:#059669; border-radius:999px; padding:2px 8px; }}
  .sources {{ margin-top:14px; display:flex; flex-wrap:wrap; gap:8px; }}
  .src {{ font-family: ui-sans-serif, system-ui, sans-serif; font-size:12.5px;
         color:var(--accent); text-decoration:none; border:1px solid var(--line);
         border-radius:6px; padding:4px 10px; background:#fff; }}
  .src:hover {{ border-color:var(--accent); }}
  footer {{ margin-top:56px; padding-top:20px; border-top:1px solid var(--line);
           font-family: ui-sans-serif, system-ui, sans-serif; font-size:12px;
           color:var(--muted); }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="kicker">Feed continu · {_esc(topic.name)}</div>
    <h1>{_esc(topic.name)}</h1>
    <div class="meta">{len(items)} sujet(s) sur les {days} derniers jours · source : {_esc(topic.source)}</div>
    {body}
    <footer>Feed maintenu automatiquement par Cardinal. Les items de plus de {days} jours sont retirés.</footer>
  </div>
</body>
</html>"""
