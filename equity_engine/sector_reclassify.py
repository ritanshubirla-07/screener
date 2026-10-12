"""
sector_reclassify.py — Lightweight Screener sector/industry reclassifier.

Fetches ONLY the company page for each symbol (no financial table parsing),
extracts breadcrumb sector + industry, and updates the companies table.
Rate-limited to ~0.7s/company to be Screener-friendly.
Skips companies already classified (non-empty sector).
Expected runtime: ~70-80 minutes for 7,000 companies.
"""
import sys
import time
import random
import datetime
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, 'c:/Users/hiten/mf-feature')
from equity_engine.database import get_connection, init_db

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5'
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=10)
SESSION.mount('https://', adapter)

def fetch_sector_industry(symbol, screener_url=None):
    """Fetches just the breadcrumbs from a Screener company page."""
    urls_to_try = []
    if screener_url:
        urls_to_try.append(screener_url)
    urls_to_try += [
        f"https://www.screener.in/company/{symbol}/consolidated/",
        f"https://www.screener.in/company/{symbol}/",
    ]
    for url in urls_to_try:
        try:
            r = SESSION.get(url, timeout=10)
            if r.status_code == 200:
                soup = BeautifulSoup(r.content, 'html.parser')
                crumbs = soup.select('.breadcrumbs a')
                sector, industry = "", ""
                if len(crumbs) >= 3:
                    sector = crumbs[-2].text.strip()
                    industry = crumbs[-1].text.strip()
                elif len(crumbs) == 2:
                    sector = crumbs[-1].text.strip()
                    industry = crumbs[-1].text.strip()
                elif len(crumbs) == 1:
                    sector = crumbs[0].text.strip()
                    industry = crumbs[0].text.strip()
                # Clean up generic Home label
                if sector.lower() in ('home', ''):
                    sector = ""
                if industry.lower() in ('home', ''):
                    industry = ""
                if sector or industry:
                    return sector, industry
        except Exception as e:
            if any(w in str(e).lower() for w in ['reset', '10054', 'connection']):
                print(f"  [NET ERROR] {symbol}: {e} — pausing 20s...")
                time.sleep(20)
    return None, None

def main():
    init_db()
    conn = get_connection()

    # Get all companies that need reclassification
    # (sector is null/empty OR industry is 'General')
    rows = conn.execute("""
        SELECT symbol, screener_url FROM companies
        WHERE sector IS NULL OR sector = '' OR industry = 'General' OR industry IS NULL OR industry = ''
        ORDER BY symbol
    """).fetchall()
    conn.close()

    total = len(rows)
    updated = 0
    skipped = 0
    start = datetime.datetime.now()

    print(f"==============================================")
    print(f"  SECTOR / INDUSTRY RECLASSIFIER")
    print(f"==============================================")
    print(f"Companies to reclassify: {total:,}")
    print(f"Started: {start.strftime('%H:%M:%S')}")
    print(f"==============================================\n")

    for i, row in enumerate(rows, 1):
        symbol = row['symbol']
        screener_url = row['screener_url']

        # Skip BSE-only 6-digit numeric codes that have no Screener URL
        if len(symbol) == 6 and symbol.isdigit() and not screener_url:
            skipped += 1
            continue

        sector, industry = fetch_sector_industry(symbol, screener_url)

        if sector or industry:
            conn2 = get_connection()
            conn2.execute(
                "UPDATE companies SET sector=?, industry=?, updated_at=CURRENT_TIMESTAMP WHERE symbol=?",
                (sector or "", industry or "", symbol)
            )
            conn2.commit()
            conn2.close()
            updated += 1

            elapsed = (datetime.datetime.now() - start).total_seconds()
            rate = updated / elapsed if elapsed > 0 else 0
            remaining = (total - i) / rate if rate > 0 else 0
            eta = datetime.datetime.now() + datetime.timedelta(seconds=remaining)
            print(f"[{i:>5}/{total}] {symbol:<14} → {sector} / {industry}  | ETA {eta.strftime('%H:%M')}")
        else:
            skipped += 1
            if i % 50 == 0:
                print(f"[{i:>5}/{total}] {symbol:<14} → no breadcrumb (BSE-only / no Screener page)")

        # Batch rest every 200 companies
        if i % 200 == 0:
            print(f"\n  [REST] Batch of 200 done. Pausing 20s...\n")
            time.sleep(20)
        else:
            time.sleep(0.6 + random.uniform(0.05, 0.2))

    elapsed_total = (datetime.datetime.now() - start).total_seconds()
    print(f"\n==============================================")
    print(f"  RECLASSIFICATION COMPLETE")
    print(f"  Updated: {updated:,} | Skipped (no page): {skipped:,}")
    print(f"  Time: {int(elapsed_total//60)}m {int(elapsed_total%60)}s")
    print(f"==============================================")

if __name__ == "__main__":
    main()
