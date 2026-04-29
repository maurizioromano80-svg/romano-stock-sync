"""
Socim B2B Discovery Script
Logs into Socim, searches each WooCommerce base code, and collects ALL
product card codes/descriptions found. Output: socim_discovery.csv
"""

import asyncio
import re
import time
import requests
import csv
from playwright.async_api import async_playwright

# ── Credentials ───────────────────────────────────────────────────────────────
CK       = 'ck_aad42597fa98979b2587fa28fb04e3417bf2bed0'
CS       = 'cs_258cb4a1358b282089fe2a3f9cdfffdf61a11fc3'
BASE_WC  = 'https://www.romanoforniture.com/wp-json/wc/v3'
AUTH     = {'consumer_key': CK, 'consumer_secret': CS}

B2B_URL  = 'https://b2b.socim.it/Web/views/web/webuplogin.jsf'
B2B_USER = 'CL026609'
B2B_PASS = 'YS584WT'

SIZE_TOKENS = {
    'XS','S','M','L','XL','XXL','XXXL','3XL','4XL','5XL','6XL',
    '35','36','37','38','39','40','41','42','43','44','45','46','47','48',
    '6','7','8','9','10','11','12','TU','UNICA'
}

def parse_base_code(sku):
    parts = sku.split('-')
    if len(parts) >= 2:
        return parts[0]
    return sku

# ── Step 1: Get all unique base codes from WooCommerce ────────────────────────
print('Fetching WooCommerce variations...')
base_codes = set()
page_num = 1
while True:
    r = requests.get(f'{BASE_WC}/products',
                     params={**AUTH, 'per_page': 100, 'page': page_num,
                             'status': 'any', 'type': 'variable'}, timeout=30)
    prods = r.json()
    if not prods:
        break
    for p in prods:
        vpage = 1
        while True:
            vr = requests.get(f'{BASE_WC}/products/{p["id"]}/variations',
                              params={**AUTH, 'per_page': 100, 'page': vpage}, timeout=30)
            vs = vr.json()
            for v in vs:
                sku = v.get('sku', '').strip()
                if sku:
                    base_codes.add(parse_base_code(sku))
            if len(vs) < 100:
                break
            vpage += 1
        time.sleep(0.05)
    print(f'  WC page {page_num}: {len(prods)} products | base codes so far: {len(base_codes)}')
    if len(prods) < 100:
        break
    page_num += 1

base_codes = sorted(base_codes)
print(f'Total unique base codes: {len(base_codes)}')

# ── Step 2: Scrape Socim for each code, collect ALL card results ───────────────
async def run_discovery():
    results = []  # [(search_code, card_code, card_desc)]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # Login
        await page.goto(B2B_URL)
        await page.wait_for_load_state('networkidle')
        await page.wait_for_timeout(2000)
        await page.fill('#loginTabView\\:j_idt70\\:j_idt74', B2B_USER)
        await page.fill('#loginTabView\\:j_idt70\\:passwordlogin', B2B_PASS)
        await page.click('button:has-text("Accedi"), button:has-text("Login"), button[id*="Login"]')
        await page.wait_for_load_state('networkidle')
        await page.wait_for_timeout(2000)
        print('Login OK')

        # Go to catalog
        cat = page.locator('a:has-text("Catalogo"), button:has-text("Catalogo"), span:has-text("Catalogo")')
        await cat.first.click()
        await page.wait_for_load_state('networkidle')
        await page.wait_for_timeout(1000)

        total = len(base_codes)
        for i, code in enumerate(base_codes):
            print(f'[{i+1}/{total}] {code}', end=' ... ', flush=True)
            try:
                # Navigate to search
                search_input = page.locator('input[name*="RICERCA"], input[id*="RICERCA"]')
                if await search_input.count() == 0 or not await search_input.first.is_visible():
                    # Try clicking catalog again
                    cat2 = page.locator('a:has-text("Catalogo"), button:has-text("Catalogo"), span:has-text("Catalogo")')
                    await cat2.first.click()
                    await page.wait_for_load_state('networkidle')
                    await page.wait_for_timeout(1000)
                    search_input = page.locator('input[name*="RICERCA"], input[id*="RICERCA"]')

                await search_input.first.fill(code)
                await search_input.first.press('Enter')
                await page.wait_for_load_state('networkidle')
                await page.wait_for_timeout(1500)

                # Collect ALL cards shown
                cards = page.locator('.IML_img')
                card_count = await cards.count()

                # Get descriptions from all cards
                descs = await page.locator('.IML_description, .IML_title, .IML_text, .IML_code').all_text_contents()
                # Also try to get codes from card containers
                card_texts = []
                for ci in range(min(card_count, 30)):
                    try:
                        # Get parent container text
                        card_container = page.locator('.IML_img').nth(ci)
                        parent_text = ''
                        try:
                            # Try to get the text of the surrounding article/item block
                            parent_text = await page.evaluate("""
                                (el) => {
                                    let p = el.closest('.IML_item, .IML_article, [class*="IML"]');
                                    return p ? p.innerText : el.closest('div').innerText;
                                }
                            """, await card_container.element_handle())
                        except Exception:
                            pass
                        card_texts.append(parent_text.strip().replace('\n', ' ')[:200])
                    except Exception:
                        card_texts.append('')

                print(f'{card_count} cards')
                for ci, ct in enumerate(card_texts):
                    results.append({
                        'search_code': code,
                        'card_index': ci,
                        'card_text': ct,
                        'all_descs': ' | '.join(descs[:20]) if ci == 0 else '',
                    })

                if card_count == 0:
                    results.append({
                        'search_code': code,
                        'card_index': -1,
                        'card_text': 'NO_RESULTS',
                        'all_descs': '',
                    })

            except Exception as e:
                print(f'ERROR: {e}')
                results.append({
                    'search_code': code,
                    'card_index': -1,
                    'card_text': f'ERROR: {e}',
                    'all_descs': '',
                })

            await page.wait_for_timeout(300)

        await browser.close()
    return results


results = asyncio.run(run_discovery())

# ── Step 3: Save results ───────────────────────────────────────────────────────
with open('socim_discovery.csv', 'w', newline='', encoding='utf-8') as f:
    writer = csv.DictWriter(f, fieldnames=['search_code', 'card_index', 'card_text', 'all_descs'])
    writer.writeheader()
    writer.writerows(results)

print(f'\nSaved {len(results)} rows to socim_discovery.csv')

# Quick analysis: codes with multiple cards (likely suffix variants)
from collections import Counter
multi = {c: n for c, n in Counter(r['search_code'] for r in results).items() if n > 1}
print(f'Codes with multiple cards: {len(multi)}')
for code, n in sorted(multi.items()):
    print(f'  {code}: {n} cards')
