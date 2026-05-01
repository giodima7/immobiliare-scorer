#!/usr/bin/env python3
"""
Debug: compare city-level (works) vs area-level (fails) Idealista URLs.
Shows page type, final URL, title, article count for each.
"""
import asyncio
import nodriver as uc
from fetch_rentals import EDGE_PATH

URLS = [
    ("city-level no-filter",  "https://www.idealista.it/affitto-case/milano-milano/con-affitto-lungo-termine/"),
    ("navigli area",           "https://www.idealista.it/affitto-case/milano-milano/navigli/con-prezzo_3000,bilocali-2,trilocali-3,quadrilocali-4,5-locali-o-piu,affitto-lungo-termine/"),
    ("navigli no-filter",      "https://www.idealista.it/affitto-case/milano-milano/navigli/"),
    ("navigli-zona",           "https://www.idealista.it/affitto-case/zona-navigli-milano/"),
]

async def check(tab, label, url):
    print(f"\n  [{label}]")
    print(f"    URL: {url}")
    await tab.get(url)
    await asyncio.sleep(15)  # wait for render

    final_url = await tab.evaluate("window.location.href")
    count     = await tab.evaluate("document.querySelectorAll('article.item').length")
    h1        = await tab.evaluate("document.querySelector('h1')?.innerText || ''")
    body_len  = await tab.evaluate("(document.body?.innerText || '').length")
    total_art = await tab.evaluate("document.querySelectorAll('article').length")

    print(f"    Final URL   : {final_url}")
    print(f"    H1          : {h1!r}")
    print(f"    Body length : {body_len}")
    print(f"    article     : {total_art}")
    print(f"    article.item: {count}")

async def run():
    browser = await uc.start(
        browser_executable_path=EDGE_PATH,
        headless=False,
        lang="it-IT",
    )
    try:
        tab = await browser.get("about:blank")
        for label, url in URLS:
            try:
                await check(tab, label, url)
            except Exception as e:
                print(f"    ERROR: {e}")
    finally:
        browser.stop()

asyncio.run(run())
