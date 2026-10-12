"""
bse_bhavcopy.py — BSE Daily Bhavcopy Fetcher & Backfiller

Fetches official BSE equity bhavcopy files for all BSE-listed scrips,
including BSE-only 6-digit numeric codes that Yahoo Finance doesn't carry.

BSE Bhavcopy URL format:
  https://www.bseindia.com/download/BhavCopy/Equity/EQ{DDMMYY}_CSV.ZIP
"""

import datetime
import io
import zipfile
import time
import requests
import pandas as pd
from .database import get_connection, init_db

BSE_BHAVCOPY_URL = "https://www.bseindia.com/download/BhavCopy/Equity/EQ{ddmmyy}_CSV.ZIP"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
    'Referer': 'https://www.bseindia.com/markets/MarketInfo/BhavCopy.aspx'
}


def fetch_bse_bhavcopy_for_date(trade_date: datetime.date) -> pd.DataFrame:
    """
    Downloads and parses BSE equity bhavcopy ZIP for a given date.
    Returns a DataFrame with OHLCV data, or empty DataFrame if unavailable.
    """
    ddmmyy = trade_date.strftime('%d%m%y')
    url = BSE_BHAVCOPY_URL.format(ddmmyy=ddmmyy)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200 or len(resp.content) < 500:
            return pd.DataFrame()

        # BSE delivers a ZIP containing a CSV
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            csv_names = [n for n in zf.namelist() if n.upper().endswith('.CSV')]
            if not csv_names:
                return pd.DataFrame()
            with zf.open(csv_names[0]) as f:
                df = pd.read_csv(f)

        df.columns = [c.strip().upper() for c in df.columns]

        # Filter to equity 'A', 'B', 'T', 'XT', 'Z' groups (exclude MF/derivatives)
        if 'SERIES' in df.columns:
            df = df[df['SERIES'].astype(str).str.strip().isin(['A', 'B', 'T', 'XT', 'Z', 'EQ'])].copy()
        elif 'GROUP' in df.columns:
            df = df[~df['GROUP'].astype(str).str.strip().isin(['MF', 'IF', 'GB', 'GS', ''])].copy()

        return df

    except Exception as e:
        return pd.DataFrame()


def ingest_bse_bhavcopy_df(df: pd.DataFrame, trade_date: datetime.date, db_path=None) -> int:
    """
    Inserts BSE bhavcopy rows into daily_market_quotes.
    Uses BSE scrip code (6-digit) as the symbol key.
    """
    if df.empty:
        return 0

    date_str = trade_date.strftime('%Y-%m-%d')
    conn = get_connection(db_path) if db_path else get_connection()
    cur = conn.cursor()

    cur.execute("SELECT symbol FROM companies")
    known_symbols = set(r[0] for r in cur.fetchall())

    def safe_float(val, default=0.0):
        try:
            v = float(str(val).replace(',', '').strip())
            return v if not pd.isna(v) else default
        except:
            return default

    def safe_int(val):
        try:
            return int(float(str(val).replace(',', '').strip()))
        except:
            return 0

    rows_to_insert = []

    # BSE bhavcopy column mapping (different versions have slightly different names)
    col_map = {}
    for c in df.columns:
        cl = c.strip().upper()
        if cl in ('SC_CODE', 'SCRIP_CODE', 'CODE'):
            col_map['code'] = c
        elif cl in ('OPEN', 'OPEN_PRICE', 'OPENPRICE'):
            col_map['open'] = c
        elif cl in ('HIGH', 'HIGH_PRICE', 'HIGHPRICE'):
            col_map['high'] = c
        elif cl in ('LOW', 'LOW_PRICE', 'LOWPRICE'):
            col_map['low'] = c
        elif cl in ('CLOSE', 'CLOSE_PRICE', 'CLOSEPRICE', 'LAST'):
            col_map['close'] = c
        elif cl in ('NO_OF_SHRS', 'VOLUME', 'TRADED_QUANTITY', 'QTY_TRADED', 'TOTTRDQTY'):
            col_map['volume'] = c

    if 'code' not in col_map or 'close' not in col_map:
        conn.close()
        return 0

    for _, row in df.iterrows():
        scrip_code = str(row[col_map['code']]).strip().zfill(6)
        if not scrip_code or scrip_code == '000000':
            continue

        cmp_val = safe_float(row.get(col_map['close'], 0))
        open_val = safe_float(row.get(col_map.get('open', ''), cmp_val), cmp_val)
        high_val = safe_float(row.get(col_map.get('high', ''), cmp_val), cmp_val)
        low_val = safe_float(row.get(col_map.get('low', ''), cmp_val), cmp_val)
        vol_val = safe_int(row.get(col_map.get('volume', ''), 0))

        if cmp_val <= 0:
            continue

        # Ensure company is registered
        if scrip_code not in known_symbols:
            sc_name = str(row.get('SC_NAME', row.get('SCRIP_NAME', row.get('NAME', scrip_code)))).strip()[:100]
            cur.execute(
                "INSERT OR IGNORE INTO companies (symbol, name, bse_code, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (scrip_code, sc_name, scrip_code)
            )
            known_symbols.add(scrip_code)

        rows_to_insert.append((scrip_code, date_str, cmp_val, open_val, high_val, low_val, vol_val, 7.0))

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

    conn.close()
    return len(rows_to_insert)


def backfill_bse_bhavcopies(start_date: datetime.date, end_date: datetime.date,
                             delay_sec: float = 0.8, db_path=None) -> int:
    """
    Backfills BSE bhavcopy data for all trading days in a date range.
    Skips weekends automatically. Handles holidays (empty response) gracefully.
    """
    total = 0
    curr = start_date
    trading_days = 0

    while curr <= end_date:
        if curr.weekday() < 5:  # Mon–Fri only
            trading_days += 1
            df = fetch_bse_bhavcopy_for_date(curr)
            if not df.empty:
                count = ingest_bse_bhavcopy_df(df, curr, db_path)
                total += count
                print(f"[BSE] [{curr}] Ingested {count:,} scrips")
            else:
                print(f"[BSE] [{curr}] No data (holiday / unavailable)")
            time.sleep(delay_sec)
        curr += datetime.timedelta(days=1)

    print(f"\n[BSE] Backfill complete: {total:,} total rows across {trading_days} trading days")
    return total


def sync_latest_bse_bhavcopy(days_back: int = 7, db_path=None):
    """Syncs the most recent available BSE bhavcopy (walks back from today)."""
    today = datetime.date.today()
    for i in range(days_back):
        d = today - datetime.timedelta(days=i)
        if d.weekday() >= 5:
            continue
        df = fetch_bse_bhavcopy_for_date(d)
        if not df.empty:
            count = ingest_bse_bhavcopy_df(df, d, db_path)
            print(f"[BSE] Synced bhavcopy for {d}: {count:,} scrips updated")
            return d, count
    print("[BSE] No recent bhavcopy found.")
    return None, 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        s = datetime.date.fromisoformat(sys.argv[1])
        e = datetime.date.fromisoformat(sys.argv[2])
        print(f"BSE Bhavcopy backfill: {s} → {e}")
        backfill_bse_bhavcopies(s, e)
    else:
        print("Syncing latest BSE bhavcopy...")
        sync_latest_bse_bhavcopy()
