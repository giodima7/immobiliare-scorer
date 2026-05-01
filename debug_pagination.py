#!/usr/bin/env python3
"""Check what pagination URL Idealista uses with path-based filter URLs."""
import asyncio
import nodriver as uc
from fetch_rentals import EDGE_PATH

URL = ("https://www.idealista.it/affitto-case/milano-milano/"
       "con-prezzo_3000,bilocali-2,trilocali-3,quadrilocali-4,"
       "5-locali-o-piu,affitto-lungo-termine/")

async def run():
    browser = await uc.start(
        browser_executable_path=EDGE_PATH,
        headless=False,
        lang="it-IT",
    )
    tab = await browser.get(URL)
    print(f"Loading: {URL}")
    await asyncio.sleep(15)

    count = await tab.evaluate("document.querySelectorAll('article.item').length")
    print(f"Articles: {count}")

    # Check all pagination-related links
    links_js = r"""
    JSON.stringify((() => {
        const selectors = [
            'a.icon-arrow-right-after',
            'li.next a',
            'a[rel="next"]',
            '.pagination-next a',
            '[class*="pag"] a',
            '.pagination a',
        ];
        const found = [];
        for (const sel of selectors) {
            const els = document.querySelectorAll(sel);
            for (const el of els) {
                const href = el.href || el.getAttribute('href') || '';
                const text = (el.innerText || el.textContent || '').trim();
                if (href) found.push({sel, href, text});
            }
        }
        return found;
    })())
    """
    links = await tab.evaluate(links_js)
    import json
    if links and isinstance(links, str):
        data = json.loads(links)
        print(f"\nPagination links found: {len(data)}")
        for item in data:
            print(f"  [{item['sel']}] text={item['text']!r} href={item['href']}")
    else:
        print(f"\nLinks result (raw): {links}")

    # Also check what the overall results count text says
    count_text = await tab.evaluate(
        "document.querySelector('[class*=\"result\"], .total-results, h1, [class*=\"count\"]')?.innerText || ''"
    )
    print(f"\nResult count text: {count_text!r}")

    # Check full URL
    final = await tab.evaluate("window.location.href")
    print(f"Final URL: {final}")

    browser.stop()

asyncio.run(run())
