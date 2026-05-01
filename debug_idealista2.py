#!/usr/bin/env python3
"""
Detailed debug for Idealista page state after nodriver navigation.
Checks: article count, page text length, body snippet, what's rendering.
"""
import asyncio, sys, time
import nodriver as uc
from fetch_rentals import EDGE_PATH

URL = "https://www.idealista.it/affitto-case/milano-milano/"

async def run():
    browser = await uc.start(
        browser_executable_path=EDGE_PATH,
        headless=False,
        lang="it-IT",
    )
    tab = await browser.get(URL)
    print(f"\n  Loaded URL: {URL}")

    for wait_sec in [3, 5, 8, 12]:
        print(f"\n  --- After {wait_sec}s total wait ---", flush=True)
        await asyncio.sleep(1)   # incremental

        # Article count
        count = await tab.evaluate(
            "document.querySelectorAll('article.item').length"
        )
        print(f"  article.item count: {count}")

        # Page URL (may have redirected)
        href = await tab.evaluate("window.location.href")
        print(f"  window.location.href: {href}")

        # Body text length
        body_len = await tab.evaluate("(document.body?.innerText || '').length")
        print(f"  body innerText length: {body_len}")

        # First 300 chars of body text
        snippet = await tab.evaluate(
            "(document.body?.innerText || '').substring(0, 300).replace(/\\n/g,' ')"
        )
        print(f"  body snippet: {snippet!r}")

        # Check for CAPTCHA indicators
        captcha = await tab.evaluate(
            "['captcha','datadome','verifica','robot'].some(k => "
            "  (document.body?.innerText||'').toLowerCase().includes(k)"
            ")"
        )
        print(f"  captcha signals: {captcha}")

        # How many article variants exist?
        for sel in ["article", "article.item", "[data-adid]", ".item-container", "section.items-list"]:
            n = await tab.evaluate(f"document.querySelectorAll('{sel}').length")
            if n:
                print(f"    selector '{sel}': {n}")

        if count and count > 0:
            print(f"\n  ✓ Got {count} listings at {wait_sec}s — stopping early")
            break

    # If we found articles, extract one sample
    sample = await tab.evaluate("""
    (() => {
        const a = document.querySelector('article.item');
        if (!a) return null;
        return {
            id: a.getAttribute('data-adid') || a.getAttribute('data-id') || '',
            text: (a.innerText || '').substring(0,200),
        };
    })()
    """)
    if sample:
        print(f"\n  Sample article: {sample}")
    else:
        print(f"\n  No article.item found — checking page source…")
        src_len = await tab.evaluate("document.documentElement.outerHTML.length")
        print(f"  HTML length: {src_len}")
        # Check for common Idealista page elements
        for check in ["#main-listing", ".items-container", ".listing", "h1"]:
            n = await tab.evaluate(f"document.querySelectorAll('{check}').length")
            if n: print(f"    '{check}' found: {n}")
        h1 = await tab.evaluate("document.querySelector('h1')?.innerText || ''")
        print(f"  h1 text: {h1!r}")

    browser.stop()
    print("\n  Done.")

asyncio.run(run())
