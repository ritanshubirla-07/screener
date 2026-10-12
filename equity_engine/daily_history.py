import datetime
import os
import sqlite3
import pandas as pd
import numpy as np
import yfinance as yf
from typing import List, Optional

from .database import get_connection, init_db

def backfill_stock_daily_history(symbols: List[str], period="max", batch_size=40, db_path=None, skip_existing=True):
    """
    Fetches real exchange-settled historical daily OHLCV price candles from Yahoo Finance (NSE).
    period='max' fetches as far back as Yahoo has data (typically 20+ years for large caps).
    skip_existing=True will skip symbols that already have 1200+ candles (already full).
    """
    init_db(db_path)
    conn = get_connection(db_path)
    cur = conn.cursor()

    cur.execute("SELECT symbol FROM companies")
    known = set(r[0] for r in cur.fetchall())

    if skip_existing:
        cur.execute("SELECT symbol FROM daily_market_quotes WHERE volume > 0 GROUP BY symbol HAVING count(*) >= 1200")
        already_full = set(r[0] for r in cur.fetchall())
    else:
        already_full = set()

    valid_symbols = [s.strip().upper() for s in symbols if s.strip() and s.strip().upper() not in already_full]
    total = len(valid_symbols)
    skipped = len(symbols) - total

    print(f"\n=======================================================")
    print(f"  NSE HISTORICAL DAILY PRICES BACKFILL (MAX PERIOD)")
    print(f"=======================================================")
    print(f"Total Symbols Requested: {len(symbols)}")
    print(f"Already Full (1200+ days) - Skipped: {skipped}")
    print(f"To Backfill Now: {total}")
    print(f"Period: {period} | Batch Size: {batch_size}")
    print(f"=======================================================\n")

    if total == 0:
        print("All symbols already have full candle history. Nothing to do.")
        conn.close()
        return 0

    total_inserted = 0

    for i in range(0, total, batch_size):
        chunk = valid_symbols[i:i+batch_size]
        yf_tickers = [f"{s}.NS" for s in chunk]
        print(f"[{i+1:>4}-{min(i+len(chunk), total):>4}/{total}] Fetching {len(chunk)} tickers (period={period})...")

        try:
            df = yf.download(yf_tickers, period=period, interval="1d", group_by="ticker", progress=False, auto_adjust=False)
            if df.empty:
                print(f"  -> No data returned for batch.")
                continue

            rows_to_insert = []

            for sym in chunk:
                if sym not in known:
                    cur.execute("INSERT OR IGNORE INTO companies (symbol, name, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)", (sym, sym))
                    known.add(sym)

                sym_df = None
                if len(chunk) == 1:
                    sym_df = df
                else:
                    ticker_key = f"{sym}.NS"
                    try:
                        if ticker_key in df.columns.get_level_values(0):
                            sym_df = df[ticker_key]
                    except Exception:
                        pass

                if sym_df is not None and not sym_df.empty:
                    sym_df = sym_df.dropna(subset=['Close'])
                    for dt, row in sym_df.iterrows():
                        date_str = dt.strftime('%Y-%m-%d')
                        c_val = float(row.get('Close', 0.0) or 0.0)
                        o_val = float(row.get('Open', c_val) or c_val)
                        h_val = float(row.get('High', c_val) or c_val)
                        l_val = float(row.get('Low', c_val) or c_val)
                        v_val = int(row.get('Volume', 0) or 0)
                        if c_val > 0:
                            rows_to_insert.append((sym, date_str, c_val, o_val, h_val, l_val, v_val, 7.0))

            if rows_to_insert:
                cur.executemany('''
                INSERT INTO daily_market_quotes (
                    symbol, date, cmp, open_price, high_price, low_price, volume, gsec_10y
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, date) DO UPDATE SET
                    cmp=excluded.cmp,
                    open_price=excluded.open_price,
                    high_price=excluded.high_price,
                    low_price=excluded.low_price,
                    volume=excluded.volume
                ''', rows_to_insert)
                conn.commit()
                total_inserted += len(rows_to_insert)
                print(f"  -> Inserted {len(rows_to_insert):,} candles across {len(chunk)} stocks.")

        except Exception as e:
            print(f"  [ERROR] Batch failed: {e}")

    conn.close()
    print(f"\n=======================================================")
    print(f"  BACKFILL COMPLETED: {total_inserted:,} total candle records added")
    print(f"=======================================================\n")
    return total_inserted

if __name__ == "__main__":
    import sys
    from .database import get_connection as gc
    conn = gc()
    # All NSE-style named tickers (not 6-digit BSE codes)
    syms = [r[0] for r in conn.execute("SELECT symbol FROM companies").fetchall()]
    conn.close()
    nse_syms = [s for s in syms if not (len(s) == 6 and s.isdigit())]
    print(f"Starting max-period backfill for {len(nse_syms)} NSE symbols...")
    backfill_stock_daily_history(nse_syms, period="max", batch_size=40)

