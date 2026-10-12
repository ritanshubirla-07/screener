import sqlite3
import os
import sys
import time

from equity_engine.calculator import run_vm_analysis
from equity_engine.database import get_connection

def rescore_all_companies():
    conn = get_connection()
    c = conn.cursor()

    # Get all companies that have both annual financials and quotes
    c.execute("""
        SELECT DISTINCT c.symbol
        FROM companies c
        JOIN annual_financials af ON c.symbol = af.symbol
        JOIN daily_market_quotes mq ON c.symbol = mq.symbol
        WHERE mq.cmp > 0
    """)
    symbols = [r[0] for r in c.fetchall()]
    conn.close()

    total = len(symbols)
    print(f"Rescoring {total:,} companies with raw-audited calculations...")

    success = 0
    start = time.time()
    for idx, sym in enumerate(symbols, 1):
        try:
            run_vm_analysis(sym)
            success += 1
        except Exception as e:
            pass
        if idx % 500 == 0 or idx == total:
            elapsed = time.time() - start
            rate = idx / elapsed if elapsed > 0 else 1
            print(f"  [{idx:,}/{total:,}] Processed... ({rate:.1f} comps/sec)")

    print(f"Rescoring complete! Successfully rescored {success:,} companies in {time.time()-start:.1f}s.")

if __name__ == "__main__":
    rescore_all_companies()
