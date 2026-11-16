#!/usr/bin/env python3
"""
Stock watcher -> notification Discord
-------------------------------------
Vérifie en boucle plusieurs pages produit et envoie un message Discord
dès qu'un article repasse en stock.

Aucune dépendance externe : Python 3.8+ suffit.

Utilisation :
    python stock_watcher.py --test     # vérifie chaque site une fois + message test Discord
    python stock_watcher.py            # surveillance en continu

Le webhook Discord peut être mis dans la variable DISCORD_WEBHOOK ci-dessous
ou dans la variable d'environnement DISCORD_WEBHOOK.
"""

import gzip
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# =====================================================================
# CONFIGURATION
# =====================================================================

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "COLLE_TON_WEBHOOK_ICI")

# Intervalle entre deux tours de vérification (secondes), + un peu d'aléatoire
CHECK_INTERVAL = 60
JITTER = 20

# Mentionner tout le monde sur le salon quand c'est en stock (@everyone)
PING_EVERYONE = True

# Pendant que l'article est en stock, renvoyer un rappel toutes les X minutes
REMINDER_MINUTES = 5

# Après combien d'erreurs/statuts "inconnus" d'affilée on te prévient
ALERT_AFTER_FAILURES = 10

# ---------------------------------------------------------------------
# Sites à surveiller.
#   out_of_stock : textes qui indiquent que c'est ÉPUISÉ (prioritaires)
#   in_stock     : textes qui indiquent que c'est DISPONIBLE
# Les textes sont comparés sans tenir compte des majuscules.
# Lance d'abord "--test" pour vérifier que chaque site est bien détecté.
# ---------------------------------------------------------------------
SITES = [
    # Le Nintendo Store place les visiteurs automatiques en salle d'attente :
    # le script ne peut pas lire sa page. Pour Nintendo, garde l'appli Nintendo Store.
    {
        "name": "Galaxus",
        "url": "https://www.galaxus.ch/fr/s1/product/nintendo-switch-2-the-legend-of-zelda-edition-game-consoles-76622991",
        "out_of_stock": ["actuellement épuisé", "actuellement en rupture", "currently out of stock", "derzeit nicht", "pas disponible"],
        "in_stock": ["ajouter au panier", "add to cart", "in den warenkorb"],
    },
    {
        "name": "Digitec",
        "url": "https://www.digitec.ch/fr/s1/product/nintendo-switch-2-the-legend-of-zelda-edition-game-consoles-76622991",
        "out_of_stock": ["actuellement épuisé", "actuellement en rupture", "currently out of stock", "derzeit nicht", "pas disponible"],
        "in_stock": ["ajouter au panier", "add to cart", "in den warenkorb"],
    },
]

STATE_FILE = Path(__file__).with_name("stock_state.json")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
]

# =====================================================================
# CODE
# =====================================================================

IN, OUT, UNKNOWN, ERROR = "EN STOCK", "ÉPUISÉ", "INCONNU", "ERREUR"

# Données structurées schema.org souvent présentes sur les fiches produit
SCHEMA_RE = re.compile(
    r"schema\.org/(InStock|OutOfStock|SoldOut|PreOrder|PreSale|BackOrder|Discontinued|LimitedAvailability)",
    re.IGNORECASE,
)
SCHEMA_IN = {"instock", "preorder", "presale", "limitedavailability"}


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def fetch(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-CH,fr;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def check_site(site):
    """Retourne (statut, détail)."""
    try:
        page = fetch(site["url"])
    except urllib.error.HTTPError as e:
        return ERROR, f"HTTP {e.code} (le site bloque peut-être les robots)"
    except Exception as e:  # réseau, timeout...
        return ERROR, f"{type(e).__name__}: {e}"

    text = html.unescape(page).lower()

    # 1) Données structurées schema.org (le plus fiable quand présent)
    found = {m.lower() for m in SCHEMA_RE.findall(text)}
    if found:
        if found & SCHEMA_IN:
            return IN, f"schema.org: {', '.join(sorted(found))}"
        return OUT, f"schema.org: {', '.join(sorted(found))}"

    # 2) Mots-clés
    for kw in site.get("out_of_stock", []):
        if kw.lower() in text:
            return OUT, f"mot-clé « {kw} »"
    for kw in site.get("in_stock", []):
        if kw.lower() in text:
            return IN, f"mot-clé « {kw} »"

    return UNKNOWN, f"aucun indicateur trouvé (page de {len(page)} caractères, peut-être chargée en JavaScript)"


def discord(content, color=None, title=None, url=None):
    if not DISCORD_WEBHOOK or "COLLE_TON_WEBHOOK" in DISCORD_WEBHOOK:
        log("⚠️  Webhook Discord non configuré, message non envoyé : " + (title or content))
        return
    payload = {"content": content, "allowed_mentions": {"parse": ["everyone"]}}
    if title:
        payload["embeds"] = [{"title": title, "url": url, "color": color or 0x2ECC71,
                              "timestamp": datetime.utcnow().isoformat()}]
    req = urllib.request.Request(
        DISCORD_WEBHOOK,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "stock-watcher/1.0"},
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        log(f"Erreur d'envoi Discord : {e}")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def run_test():
    log("Mode test : une vérification par site")
    for site in SITES:
        status, detail = check_site(site)
        log(f"{site['name']:<22} -> {status:<9} ({detail})")
        if status in (UNKNOWN, ERROR) and "--debug" in sys.argv:
            try:
                print("----- DÉBUT DE LA PAGE -----\n" + fetch(site["url"])[:6000] + "\n----- FIN -----")
            except Exception as e:
                print(f"(page illisible : {e})")
    discord("✅ Test du stock watcher : les notifications Discord fonctionnent.")
    log("Message test envoyé sur Discord (si le webhook est configuré).")


def run_forever(duration=None):
    """duration = None : tourne sans fin. Sinon s'arrête après `duration` secondes
    (utilisé par GitHub Actions, qui relance le script toutes les 5 minutes)."""
    state = load_state()
    mention = "@everyone " if PING_EVERYONE else ""
    start = time.time()
    if duration is None:
        log(f"Surveillance de {len(SITES)} site(s), toutes les ~{CHECK_INTERVAL}s. Ctrl+C pour arrêter.")
        discord(f"👀 Surveillance démarrée sur {len(SITES)} site(s).")
    else:
        log(f"Tour de surveillance de {duration}s sur {len(SITES)} site(s).")

    while duration is None or time.time() - start < duration:
        for site in SITES:
            name = site["name"]
            s = state.setdefault(name, {"status": None, "fails": 0, "last_alert": 0, "warned": False})
            status, detail = check_site(site)
            log(f"{name:<22} -> {status:<9} ({detail})")

            now = time.time()
            if status == IN:
                s["fails"], s["warned"] = 0, False
                is_new = s["status"] != IN
                if is_new or now - s["last_alert"] >= REMINDER_MINUTES * 60:
                    prefix = "🚨 EN STOCK" if is_new else "⏰ Toujours en stock"
                    discord(f"{mention}{prefix} chez **{name}** !\n{site['url']}",
                            title=f"{prefix} – {name}", url=site["url"], color=0x2ECC71)
                    s["last_alert"] = now
            elif status == OUT:
                s["fails"], s["warned"] = 0, False
                if s["status"] == IN:
                    discord(f"❌ {name} : de nouveau épuisé.")
            else:  # UNKNOWN / ERROR
                s["fails"] += 1
                if s["fails"] >= ALERT_AFTER_FAILURES and not s["warned"]:
                    discord(f"⚠️ {name} : impossible de lire le stock depuis "
                            f"{s['fails']} essais ({detail}). Vérifie la config.")
                    s["warned"] = True

            if status in (IN, OUT):
                s["status"] = status
            save_state(state)
            time.sleep(random.uniform(2, 6))  # petite pause entre les sites

        pause = CHECK_INTERVAL + random.uniform(0, JITTER)
        if duration is not None and time.time() - start + pause >= duration:
            break
        time.sleep(pause)


if __name__ == "__main__":
    dur = None
    if "--duration" in sys.argv:
        dur = int(sys.argv[sys.argv.index("--duration") + 1])
    try:
        run_test() if "--test" in sys.argv else run_forever(dur)
    except KeyboardInterrupt:
        log("Arrêt.")
