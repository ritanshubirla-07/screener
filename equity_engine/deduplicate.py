import sqlite3
import re
import os
import sys

def normalize_name(name):
    if not name:
        return ""
    s = name.lower()
    s = re.sub(r'\b(ltd|limited|pvt|private|corp|corporation|inc|incorporated)\b', '', s)
    s = re.sub(r'[^a-z0-9]', '', s)
    return s.strip()

def deduplicate_universe(db_path=None):
    if not db_path:
        db_path = os.path.join(os.path.dirname(__file__), "equity_master.db")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    print("=" * 65)
    print("  DEDUPLICATING UNIVERSE: MERGING BSE NUMERIC SCRIPS INTO NSE TICKERS")
    print("=" * 65)

    # 1. Fetch named tickers
    c.execute("SELECT symbol, name, isin, bse_code FROM companies WHERE NOT (length(symbol) = 6 AND (symbol LIKE '5%' OR symbol LIKE '4%'))")
    named_comps = c.fetchall()

    # 2. Fetch BSE numeric codes
    c.execute("SELECT symbol, name, isin, bse_code FROM companies WHERE length(symbol) = 6 AND (symbol LIKE '5%' OR symbol LIKE '4%')")
    bse_comps = c.fetchall()

    print(f"Named tickers before: {len(named_comps):,}")
    print(f"BSE numeric scrips before: {len(bse_comps):,}")

    named_by_norm = {}
    for comp in named_comps:
        norm = normalize_name(comp['name'])
        if norm and norm not in named_by_norm:
            named_by_norm[norm] = comp

    merged_count = 0
    child_tables = ['annual_financials', 'quarterly_financials', 'shareholding', 'daily_market_quotes', 'analysis_results', 'ingestion_log']

    for b in bse_comps:
        norm = normalize_name(b['name'])
        if norm in named_by_norm:
            named = named_by_norm[norm]
            named_sym = named['symbol']
            bse_sym = b['symbol']

            # 1. Update named company's bse_code
            c.execute("UPDATE companies SET bse_code = ? WHERE symbol = ?", (bse_sym, named_sym))

            # 2. If named is missing isin, inherit from bse if present
            if (not named['isin']) and b['isin']:
                c.execute("UPDATE companies SET isin = ? WHERE symbol = ?", (b['isin'], named_sym))

            # 3. For each child table, ensure named has data, then delete bse rows
            for table in child_tables:
                # Check if named has rows
                c.execute(f"SELECT count(*) FROM {table} WHERE symbol = ?", (named_sym,))
                named_has_rows = c.fetchone()[0] > 0

                if named_has_rows:
                    # Named already has data, safe to delete redundant BSE rows
                    c.execute(f"DELETE FROM {table} WHERE symbol = ?", (bse_sym,))
                else:
                    # Named had no rows, migrate BSE rows to named symbol!
                    c.execute(f"UPDATE OR IGNORE {table} SET symbol = ? WHERE symbol = ?", (named_sym, bse_sym))
                    c.execute(f"DELETE FROM {table} WHERE symbol = ?", (bse_sym,))

            # 4. Delete BSE numeric record from companies
            c.execute("DELETE FROM companies WHERE symbol = ?", (bse_sym,))
            merged_count += 1

    conn.commit()

    # Check remaining counts
    c.execute("SELECT count(*) FROM companies")
    total_after = c.fetchone()[0]

    c.execute("SELECT count(*) FROM companies WHERE length(symbol) = 6 AND (symbol LIKE '5%' OR symbol LIKE '4%')")
    bse_after = c.fetchone()[0]

    c.execute("SELECT count(*) FROM companies WHERE NOT (length(symbol) = 6 AND (symbol LIKE '5%' OR symbol LIKE '4%'))")
    named_after = c.fetchone()[0]

    print(f"\nSuccessfully merged and deduplicated {merged_count:,} duplicate pairs!")
    print(f"Total Companies now: {total_after:,}")
    print(f"  Named Tickers: {named_after:,}")
    print(f"  BSE-only Numeric Scrips: {bse_after:,} (pure BSE companies with no NSE counterpart)")
    print("=" * 65)

    conn.close()

if __name__ == "__main__":
    deduplicate_universe()
