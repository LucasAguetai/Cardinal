"""
Définition des sujets de veille.

Un "sujet" = quoi surveiller + à quelle fréquence + via quelle source.
- source="web" : tu fournis des flux RSS (gratuits) dans `feeds`.
- source="osv" : tu fournis tes paquets dans `packages` (CVE via OSV.dev).
"""

from dataclasses import dataclass, field


@dataclass
class Topic:
    id: str
    name: str
    source: str                  # "web" (RSS) | "osv" (CVE deps) | "nvd" (CVE produits)
    frequency_hours: int         # fenêtre de veille (les X dernières heures)
    feeds: list = field(default_factory=list)     # source="web" : URLs de flux RSS
    packages: list = field(default_factory=list)  # source="osv" : paquets surveillés
    keywords: list = field(default_factory=list)  # source="nvd" : produits (mots-clés)
    min_severity: str = ""       # source="nvd" : "" | LOW | MEDIUM | HIGH | CRITICAL


# --- Tes sujets ------------------------------------------------------------
# Un flux RSS se trouve souvent en ajoutant /feed ou /rss à un site,
# ou dans son pied de page. Ajuste librement.

DEFAULT_TOPICS = {
    "ar": Topic(
        id="ar",
        name="Réalité augmentée / XR",
        source="web",
        frequency_hours=72,
        feeds=[
            "https://www.roadtovr.com/feed/",
            "https://www.uploadvr.com/rss/",
        ],
    ),
    "gamedev": Topic(
        id="gamedev",
        name="Jeux vidéo",
        source="web",
        frequency_hours=24,
        feeds=[
            "https://www.rockpapershotgun.com/feed",
            "https://www.pcgamer.com/rss/",
            "https://www.eurogamer.net/feed",
        ],
    ),
    # Veille CVE PRODUITS (systèmes, logiciels) via NVD, par mots-clés.
    # Astuce : préfixe par le fabricant pour réduire le bruit ("Apple iOS" > "iOS").
    # min_severity="HIGH" pour ne garder que le haut du panier (optionnel).
    "produits": Topic(
        id="produits",
        name="Nouvelles CVE — mes produits",
        source="nvd",
        frequency_hours=72,
        min_severity="",  # ex. "HIGH" pour filtrer
        keywords=[
            "Apple iOS",
            "Apple macOS",
            "Windows 11",
            "Python",          # bruité (langage) — le LLM écarte les faux positifs
            "CentOS Stream 9",
            "Ubuntu",
            "WireGuard",
            "RealVNC",         # précise TON implémentation VNC (TigerVNC, UltraVNC...)
        ],
    ),

    # Freebox Ultra : pas de base CVE dédiée -> veille via RSS actu.
    "freebox": Topic(
        id="freebox",
        name="Freebox Ultra",
        source="web",
        frequency_hours=168,
        feeds=[
            "https://www.universfreebox.com/feed",  # ajuste selon tes sources
        ],
    ),

    # Veille CVE DÉPENDANCES (librairies de code) via OSV : liste les paquets que TU utilises.
    # ecosystem = valeur OSV valide : PyPI, npm, Go, crates.io, Maven, NuGet,
    # RubyGems, Packagist, Debian, Ubuntu, etc.
    "mesvulns": Topic(
        id="mesvulns",
        name="Nouvelles CVE — ma stack",
        source="osv",
        frequency_hours=168,  # 7 jours
        packages=[
            {"name": "requests", "ecosystem": "PyPI"},
            {"name": "django", "ecosystem": "PyPI"},
            {"name": "express", "ecosystem": "npm"},
            {"name": "next", "ecosystem": "npm"},
        ],
    ),
}

