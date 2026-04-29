"""
Socim B2B -> WooCommerce Stock Sync
Reads per-size availability from B2B and updates WooCommerce variation stock_status.

WC variation SKU format:
  {b2b_code}-{COLOR}-{SIZE}  (3 parts, e.g. E0550-GRIGIO-XS)
  {b2b_code}-{SIZE}          (2 parts, e.g. SZ300-48)

B2B: click color variant by matching color word in description, then read stock per size.
Skips "prodotti stock" (discontinued) items.
"""

import asyncio
import re
import time
import requests
from collections import defaultdict
from playwright.async_api import async_playwright

# ── WooCommerce ──────────────────────────────────────────────────────────────
CK   = 'ck_aad42597fa98979b2587fa28fb04e3417bf2bed0'
CS   = 'cs_258cb4a1358b282089fe2a3f9cdfffdf61a11fc3'
BASE = 'https://www.romanoforniture.com/wp-json/wc/v3'
AUTH = {'consumer_key': CK, 'consumer_secret': CS}

# ── B2B ──────────────────────────────────────────────────────────────────────
B2B_URL  = 'https://b2b.socim.it/Web/views/web/webuplogin.jsf'
B2B_USER = 'CL026609'
B2B_PASS = 'YS584WT'

# Size tokens (to distinguish size from color in 2-part SKUs)
SIZE_TOKENS = {
    'XS','S','M','L','XL','XXL','XXXL','3XL','4XL','5XL','6XL',
    '35','36','37','38','39','40','41','42','43','44','45','46','47','48',
    '6','7','8','9','10','11','12',
    'TU','UNICA'
}

def parse_variation_sku(sku: str):
    """
    Parse WC variation SKU into (b2b_code, color_or_None, size).
    Returns (code, color, size) or None if unparseable.
    """
    parts = sku.split('-')
    if len(parts) == 3:
        code, color, size = parts
        if code and size:
            return code, color.upper() if color else None, size.upper()
    elif len(parts) == 2:
        code, second = parts
        if not code or not second:
            return None
        if second.upper() in SIZE_TOKENS or second.isdigit():
            return code, None, second.upper()
        else:
            return code, second.upper(), None
    return None

def parse_qty(s: str) -> int:
    try:
        return int(str(s).replace('.', '').replace(',', '.').strip())
    except:
        return 0

# Normalizza varianti di nome taglia allo stesso token canonico
_SIZE_NORMALIZE = {
    'XXXL': '3XL', 'XXXXL': '4XL', 'XXXXXL': '5XL',
    '3XL': '3XL', '4XL': '4XL', '5XL': '5XL',
    'XXL': 'XXL', 'XL': 'XL', 'L': 'L', 'M': 'M', 'S': 'S', 'XS': 'XS',
}

def normalize_size(s: str) -> str:
    if not s:
        return s
    u = s.upper().strip()
    return _SIZE_NORMALIZE.get(u, u)

def get_stock_for_size(sizes_data: dict, size: str) -> int:
    """Look up stock by size, normalizing synonyms (e.g. XXXL == 3XL)."""
    if not size:
        return sum(sizes_data.values())
    norm = normalize_size(size)
    # Try exact match first, then normalized
    if size in sizes_data:
        return sizes_data[size]
    if norm in sizes_data:
        return sizes_data[norm]
    # Try reverse: normalize each key from sizes_data
    for k, v in sizes_data.items():
        if normalize_size(k) == norm:
            return v
    return 0

# Groups of equivalent color names (any two in the same group are considered a match)
_COLOR_SYNONYMS = [
    {'NERO', 'NERA', 'BLACK', 'NERO/GRIGIO', 'NERO/ROSSO'},
    {'ROSSO', 'ROSSA', 'RED', 'BORDEAUX', 'BORDO'},
    {'AZZURRO', 'AZZURRA', 'AZZURRO ROYAL', 'CELESTE'},
    {'GRIGIO', 'GRIGIA', 'GRIGIO ANTRACITE', 'GRIGIO MELANGE', 'GRIGIO CHIARO',
     'GRIGIO SCURO', 'ANTRACITE', 'GREY', 'GRAY', 'MELANGE'},
    {'BIANCO', 'BIANCA', 'WHITE', 'BIANCO/BLU', 'BIANCO/ROSSO'},
    {'BLU', 'BLUE', 'NAVY', 'BLU NAVY', 'BLU ROYAL', 'BLU OTTANIO',
     'BLU/NERO', 'BLU/ARANCIO', 'BLU/GIALLO'},
    {'VERDE', 'MILITARE', 'VERDE MILITARE', 'VERDE BOSCO', 'VERDE FLUO',
     'VERDE ARMY', 'FOREST', 'MIMETICO'},
    {'GIALLO', 'GIALLO FLUO', 'YELLOW', 'FLUO GIALLO'},
    {'ARANCIO', 'ARANCIO FLUO', 'ORANGE', 'FLUO ARANCIO', 'ARANCIONE'},
    {'MARRONE', 'BROWN', 'TABACCO', 'CUOIO', 'NOCCIOLA'},
    {'BEIGE', 'SABBIA', 'SAND', 'CORDA'},
    {'VIOLA', 'PURPLE', 'LILLA', 'LAVANDA'},
    {'ROSA', 'PINK', 'FUCSIA'},
]

def colors_match(wc_color: str, b2b_color: str) -> bool:
    """Match color names handling Italian variants, synonyms, and partial matches."""
    if not wc_color or not b2b_color:
        return False
    wc  = wc_color.upper().strip()
    b2b = b2b_color.upper().strip()
    if wc == b2b:
        return True
    # Contenimento: "GRIGIO" matcha "GRIGIO ANTRACITE"
    if wc in b2b or b2b in wc:
        return True
    # Sinonimi
    for group in _COLOR_SYNONYMS:
        if wc in group and b2b in group:
            return True
    # Prefisso 4 caratteri
    if len(wc) >= 4 and len(b2b) >= 4 and wc[:4] == b2b[:4]:
        return True
    return False

async def parse_stock_from_page(page) -> dict:
    """Return {size: qty} from the B2B variant detail page."""
    html = await page.content()
    pc_matches = re.findall(r'Taglia\s+(\w+)\s+-\s+P\.C\.\s+([\d.]+)', html)
    if pc_matches:
        return {size: parse_qty(qty) for size, qty in pc_matches}
    qty_matches = re.findall(
        r'data-column="TAGLIA"[^>]*title="Taglia:\s*([^"]+)"'
        r'.*?data-column="QTA00001"[^>]*title=":\s*([^"]*)"',
        html, re.DOTALL
    )
    if qty_matches:
        return {s.strip(): parse_qty(q) for s, q in qty_matches}
    return {}

async def js_click_back(page):
    r = await page.evaluate("""
        () => {
            var btn = document.getElementById('webup_action_menu:idListButton');
            if (!btn) return 'NOT_FOUND';
            btn.disabled = false;
            btn.removeAttribute('disabled');
            btn.setAttribute('aria-disabled', 'false');
            btn.click();
            return 'OK';
        }
    """)
    await page.wait_for_load_state('networkidle')
    await page.wait_for_timeout(2000)
    return r

async def check_session_expired(page) -> bool:
    """Return True if the session-expired overlay is covering the page."""
    try:
        return await page.locator('#sessionExpiredDialog_modal').is_visible(timeout=1000)
    except Exception:
        return False

async def relogin(page):
    """Re-authenticate on session expiry, then navigate to Catalogo."""
    print('    SESSION EXPIRED — re-logging in...')
    await page.goto(B2B_URL, wait_until='networkidle')
    await page.wait_for_timeout(1000)
    await page.fill('#loginTabView\\:j_idt70\\:j_idt74', B2B_USER)
    await page.fill('#loginTabView\\:j_idt70\\:passwordlogin', B2B_PASS)
    await page.click('button:has-text("Accedi"), button:has-text("Login"), button[id*="Login"]')
    await page.wait_for_load_state('networkidle')
    await page.wait_for_timeout(2000)
    cat = page.locator('a:has-text("Catalogo"), button:has-text("Catalogo"), span:has-text("Catalogo")')
    if await cat.count() > 0:
        await cat.first.click()
        await page.wait_for_load_state('networkidle')
        await page.wait_for_timeout(1000)
    print('    Re-login OK')

async def navigate_to_search(page):
    """Get back to catalog search from any state; re-login if session expired."""
    if await check_session_expired(page):
        await relogin(page)
    search_input = page.locator('input[name*="RICERCA"], input[id*="RICERCA"]')
    for _ in range(4):
        if await search_input.count() > 0 and await search_input.first.is_visible():
            return True
        r = await js_click_back(page)
        if r == 'NOT_FOUND':
            break
    # Last resort: click Catalogo
    cat = page.locator('a:has-text("Catalogo"), button:has-text("Catalogo"), span:has-text("Catalogo")')
    if await cat.count() > 0:
        await cat.first.click()
        await page.wait_for_load_state('networkidle')
        await page.wait_for_timeout(1000)
    return await search_input.count() > 0

# ── 1. Fetch WooCommerce data ─────────────────────────────────────────────────

print('=' * 60)
print('STEP 1: Reading WooCommerce variations...')
print('=' * 60)

# {(prod_id, var_id): (sku, code, color, size)}
def wc_get(url, params, retries=4):
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            wait = attempt * 5
            print(f'  WC GET error (attempt {attempt}/{retries}): {str(e)[:80]}')
            if attempt < retries:
                time.sleep(wait)
            else:
                raise

variation_data = {}
page_num = 1
while True:
    prods = wc_get(f'{BASE}/products',
                   {**AUTH, 'per_page': 100, 'page': page_num, 'status': 'any', 'type': 'variable'})
    if not prods:
        break
    for p in prods:
        # Fetch all pages of variations (products can have >100)
        vpage = 1
        while True:
            batch = wc_get(f'{BASE}/products/{p["id"]}/variations',
                           {**AUTH, 'per_page': 100, 'page': vpage})
            for v in batch:
                sku = v.get('sku', '').strip()
                if not sku:
                    continue
                parsed = parse_variation_sku(sku)
                if parsed:
                    code, color, size = parsed
                    variation_data[(p['id'], v['id'])] = (sku, code, color, size)
            if len(batch) < 100:
                break
            vpage += 1
        time.sleep(0.1)
    print(f'  Page {page_num}: {len(prods)} products')
    if len(prods) < 100:
        break
    page_num += 1

print(f'Total parseable variations: {len(variation_data)}')

# Group by B2B code: code -> list of (prod_id, var_id, color, size, sku)
by_code = defaultdict(list)
for (prod_id, var_id), (sku, code, color, size) in variation_data.items():
    by_code[code].append((prod_id, var_id, color, size, sku))

print(f'Unique B2B product codes: {len(by_code)}')

# ── 2. B2B scraping ──────────────────────────────────────────────────────────

async def scrape_all() -> dict:
    """Returns {b2b_code: {color_or_NONE: {size: qty}}}"""
    result = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()

        # Login
        await page.goto(B2B_URL, wait_until='networkidle')
        await page.fill('#loginTabView\\:j_idt70\\:j_idt74', B2B_USER)
        await page.fill('#loginTabView\\:j_idt70\\:passwordlogin', B2B_PASS)
        await page.click('button:has-text("Accedi"), button:has-text("Login"), button[id*="Login"]')
        await page.wait_for_load_state('networkidle')
        await page.wait_for_timeout(2000)
        print('  B2B login OK')

        # Go to Catalogo
        cat = page.locator('a:has-text("Catalogo"), button:has-text("Catalogo"), span:has-text("Catalogo")')
        await cat.first.click()
        await page.wait_for_load_state('networkidle')
        await page.wait_for_timeout(1000)

        codes = sorted(by_code.keys())
        total = len(codes)
        for i, code in enumerate(codes):
            print(f'\n  [{i+1}/{total}] {code}...', flush=True)
            try:
                code_result = await scrape_code(page, code)
                if code_result is not None:
                    result[code] = code_result
            except Exception as e:
                print(f'    ERROR: {e}')
            await page.wait_for_timeout(200)

        await browser.close()
    return result


async def scrape_code(page, code: str) -> dict | None:
    """
    Scrape stock for all color variants of a B2B product code.
    Returns {color_name_upper: {size: qty}} or None if not found.
    """
    await navigate_to_search(page)

    search_input = page.locator('input[name*="RICERCA"], input[id*="RICERCA"]')
    if await search_input.count() == 0 or not await search_input.first.is_visible():
        return None

    await search_input.first.fill(code)
    await search_input.first.press('Enter')
    await page.wait_for_load_state('networkidle')
    await page.wait_for_timeout(2000)

    # Check for product card
    cards = page.locator('.IML_img')
    if await cards.count() == 0:
        return None

    # Skip stock/clearance products
    card_desc = await page.locator('.IML_description, .IML_title, .IML_text').all_text_contents()
    if any('STOCK' in d.upper() for d in card_desc):
        print(f'    Skipping STOCK product: {code}')
        return None

    # Session may expire between search and card click — check and renew
    if await check_session_expired(page):
        await relogin(page)
        si2 = page.locator('input[name*="RICERCA"], input[id*="RICERCA"]')
        if await si2.count() == 0 or not await si2.first.is_visible():
            return None
        await si2.first.fill(code)
        await si2.first.press('Enter')
        await page.wait_for_load_state('networkidle')
        await page.wait_for_timeout(2000)
        cards = page.locator('.IML_img')
        if await cards.count() == 0:
            return None

    # Click card
    await cards.first.click()
    await page.wait_for_load_state('networkidle')
    await page.wait_for_timeout(2500)

    # Find color variant table
    dt = page.locator('table[role="grid"]').filter(has=page.locator('th[aria-label="Articolo"]'))
    has_colors = await dt.count() > 0

    code_data = {}

    if has_colors:
        # Get all color variant rows
        variant_rows = dt.first.locator('tr.ui-datatable-selectable')
        row_count = await variant_rows.count()
        if row_count == 0:
            variant_rows = dt.first.locator('tr.ui-widget-content')
            row_count = await variant_rows.count()

        # Determine which colors we need for this code
        needed_colors = {(c.upper() if c else None) for _, _, c, _, _ in by_code[code]}

        # Build color-to-row-index map from B2B
        # First pass: read all row text (all cells) without clicking
        b2b_color_rows = {}  # color_word -> row_idx
        COLOR_KEYWORDS = [
            'ARANCIO', 'VERDE', 'GIALLO', 'BLU', 'BLUE', 'BIANCO', 'BIANCA',
            'NERO', 'NERA', 'ROSSO', 'ROSSA', 'GRIGIO', 'GRIGIA', 'ROSA',
            'VIOLA', 'BORDEAUX', 'BEIGE', 'MARRONE', 'AZZURRO', 'AZZURRA',
            'NAVY', 'MILITARE', 'ROYAL', 'FLUO', 'CAFFE', 'SABBIA',
            'GIALLO FLUO', 'ARANCIO FLUO',
        ]
        for row_idx in range(row_count):
            dt2 = page.locator('table[role="grid"]').filter(has=page.locator('th[aria-label="Articolo"]'))
            rows2 = dt2.first.locator('tr.ui-datatable-selectable')
            if await rows2.count() == 0:
                rows2 = dt2.first.locator('tr.ui-widget-content')
            if row_idx >= await rows2.count():
                break
            row = rows2.nth(row_idx)
            # Read ALL cells of the row to maximize color detection
            try:
                row_text = (await row.text_content(timeout=5000) or '').strip().upper()
            except Exception:
                row_text = ''

            # Extract color words from full row text
            for color in COLOR_KEYWORDS:
                if color in row_text:
                    if color not in b2b_color_rows:
                        b2b_color_rows[color] = row_idx
                    break

        # If still no colors found but rows exist, map row index by position
        # using needed_colors sorted list (best-effort fallback)
        if not b2b_color_rows and row_count > 0:
            needed_list = sorted([c for c in needed_colors if c is not None])
            for idx, nc in enumerate(needed_list):
                if idx < row_count:
                    b2b_color_rows[nc] = idx
            if needed_list:
                print(f'    Fallback: mapping {needed_list} to rows 0..{min(len(needed_list),row_count)-1}')

        # Also check if we need "no color" (None) - click first row
        if None in needed_colors and row_count > 0:
            b2b_color_rows[None] = 0

        print(f'    B2B colors: {list(b2b_color_rows.keys())} | needed: {needed_colors}')

        # Second pass: click needed color rows and read stock
        colors_to_scrape = set()
        for needed_color in needed_colors:
            if needed_color is None:
                colors_to_scrape.add(None)
            else:
                # Find matching B2B color (handles ROSSO/ROSSA, AZZURRO/AZZURRA etc.)
                matched = False
                for b2b_color in b2b_color_rows:
                    if b2b_color and colors_match(needed_color, b2b_color):
                        colors_to_scrape.add(b2b_color)
                        matched = True
                        break
                if not matched:
                    print(f'    No B2B match for color "{needed_color}"')

        async def nav_to_product(pg, search_code):
            """Re-navigate fresh to product detail page; handles session expiry."""
            await navigate_to_search(pg)  # navigate_to_search already calls check_session_expired
            si = pg.locator('input[name*="RICERCA"], input[id*="RICERCA"]')
            if await si.count() == 0 or not await si.first.is_visible():
                return False
            await si.first.fill(search_code)
            await si.first.press('Enter')
            await pg.wait_for_load_state('networkidle')
            await pg.wait_for_timeout(2000)
            cards2 = pg.locator('.IML_img')
            if await cards2.count() == 0:
                return False
            # Session may expire between search and card click
            if await check_session_expired(pg):
                await relogin(pg)
                si2 = pg.locator('input[name*="RICERCA"], input[id*="RICERCA"]')
                await si2.first.fill(search_code)
                await si2.first.press('Enter')
                await pg.wait_for_load_state('networkidle')
                await pg.wait_for_timeout(2000)
                cards2 = pg.locator('.IML_img')
                if await cards2.count() == 0:
                    return False
            await cards2.first.click()
            await pg.wait_for_load_state('networkidle')
            await pg.wait_for_timeout(2500)
            return True

        first_color = True
        for target_color in sorted(colors_to_scrape, key=lambda x: (x is None, x)):
            row_idx = b2b_color_rows.get(target_color, 0)

            # After first color, re-navigate to product detail (more reliable than js_back)
            if not first_color:
                ok = await nav_to_product(page, code)
                if not ok:
                    print(f'    Could not re-navigate for {target_color}')
                    continue
            first_color = False

            dt3 = page.locator('table[role="grid"]').filter(has=page.locator('th[aria-label="Articolo"]'))
            rows3 = dt3.first.locator('tr.ui-datatable-selectable')
            if await rows3.count() == 0:
                rows3 = dt3.first.locator('tr.ui-widget-content')
            actual_count = await rows3.count()
            if actual_count == 0 or row_idx >= actual_count:
                print(f'    Row {row_idx} not available for {target_color}')
                continue

            await rows3.nth(row_idx).click()
            await page.wait_for_load_state('networkidle')
            await page.wait_for_timeout(6000)

            sizes = await parse_stock_from_page(page)
            total = sum(sizes.values())
            status = 'instock' if total > 0 else 'outofstock'
            print(f'    Color {target_color}: {status} (total={total})')
            code_data[target_color] = sizes

    else:
        # No color variant table - product might show sizes directly
        # or there's only one variant to click
        # Try reading stock directly from product page
        sizes = await parse_stock_from_page(page)
        if sizes:
            code_data[None] = sizes
            print(f'    No color table, direct stock: {sum(sizes.values())} total')
        else:
            print(f'    No color table, no stock data found')
            # Save HTML snippet for diagnosis (first 3000 chars, stripped)
            try:
                html2 = await page.content()
                snippet = ' '.join(html2.split())[:3000]
                with open('no_color_table.log', 'a', encoding='utf-8') as f:
                    f.write(f'\n\n=== {code} ===\n{snippet}\n')
            except Exception:
                pass

    # Navigate back to catalog
    await js_click_back(page)
    return code_data if code_data else None


print('\n' + '=' * 60)
print('STEP 2: Scraping B2B stock...')
print('=' * 60)

b2b_data = asyncio.run(scrape_all())
print(f'\nB2B data collected for {len(b2b_data)} product codes')

# ── 3. Map B2B stock to WooCommerce variations ───────────────────────────────

print('\n' + '=' * 60)
print('STEP 3: Building WooCommerce updates...')
print('=' * 60)

updates_by_product = defaultdict(list)
not_found = []
matched = 0

for (prod_id, var_id), (sku, code, color, size) in variation_data.items():
    code_data = b2b_data.get(code)
    if code_data is None:
        not_found.append(sku)
        continue

    stock_qty = None

    if color is None:
        # 2-part SKU: no color, sum all colors for this size
        sizes_data = code_data.get(None) or {}
        if not sizes_data:
            # Try summing across all color keys
            for color_key, sizes in code_data.items():
                if size:
                    v = get_stock_for_size(sizes, size)
                    if v:
                        stock_qty = (stock_qty or 0) + v
        else:
            stock_qty = get_stock_for_size(sizes_data, size)
    else:
        # 3-part SKU: match by color word
        color_key = None
        for b2b_color in code_data:
            if b2b_color and color in b2b_color:
                color_key = b2b_color
                break
        if color_key is None and None in code_data:
            color_key = None
        elif color_key is None:
            # Try fuzzy match
            for bk in code_data:
                if bk and colors_match(color, bk):
                    color_key = bk
                    break

        if color_key is not None or None in code_data:
            sizes_data = code_data.get(color_key, {})
            stock_qty = get_stock_for_size(sizes_data, size)

    if stock_qty is None:
        not_found.append(sku)
        continue

    matched += 1
    updates_by_product[prod_id].append({
        'id': var_id,
        'stock_status': 'instock' if stock_qty > 0 else 'outofstock',
        'manage_stock': True,
        'stock_quantity': stock_qty
    })

print(f'Matched: {matched} | Not found in B2B: {len(not_found)}')

# ── 4. Update WooCommerce ────────────────────────────────────────────────────

print('\n' + '=' * 60)
print('STEP 4: Updating WooCommerce...')
print('=' * 60)

ok_count = 0
err_count = 0

for prod_id, var_updates in updates_by_product.items():
    for i in range(0, len(var_updates), 100):
        batch = var_updates[i:i+100]
        r = requests.post(
            f'{BASE}/products/{prod_id}/variations/batch',
            params=AUTH,
            json={'update': batch},
            timeout=60
        )
        if r.status_code == 200:
            n = len(r.json().get('update', []))
            ok_count += n
        else:
            err_count += len(batch)
            print(f'  Product {prod_id} ERROR {r.status_code}: {r.text[:150]}')
        time.sleep(0.5)

# ── 5. Summary ───────────────────────────────────────────────────────────────

print('\n' + '=' * 60)
print('RIEPILOGO FINALE')
print('=' * 60)
print(f'Variazioni WooCommerce totali:  {len(variation_data)}')
print(f'Variazioni matchate con B2B:    {matched}')
print(f'Variazioni aggiornate OK:       {ok_count}')
print(f'Errori aggiornamento:           {err_count}')
print(f'Non trovate su B2B:             {len(not_found)}')
if not_found[:10]:
    print(f'\nPrime SKU non trovate:')
    for s in not_found[:10]:
        print(f'  - {s}')
