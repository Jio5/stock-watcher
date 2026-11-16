import re, sys
from playwright.sync_api import sync_playwright
URLS = {
 "Galaxus": "https://www.galaxus.ch/fr/s1/product/nintendo-switch-2-the-legend-of-zelda-edition-game-consoles-76622991",
 "Nintendo": "https://store.nintendo.com/fr-ch/nintendo-switch-2-edition-40e-anniversaire-de-the-legend-of-zelda-P00211",
}
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    pg = b.new_page(locale="fr-CH")
    for n, u in URLS.items():
        try:
            pg.goto(u, wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(8000)
            txt = re.sub(r"\s+", " ", pg.inner_text("body"))
            print(f"== {n} | {pg.url[:90]} | titre: {pg.title()}")
            print(txt[:1200])
            print("schema:", re.findall(r"schema\.org/(InStock|OutOfStock|SoldOut|PreOrder|BackOrder)", pg.content()))
        except Exception as e:
            print(f"== {n} ERREUR {e}")
    b.close()
