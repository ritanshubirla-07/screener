import datetime
import io
import re
import pandas as pd
from typing import Optional, List
import jugaad_data.nse as jnse
from .database import get_connection, init_db

def clean_val(v):
    if v is None or pd.isna(v):
        return 0.0
    s = str(v).replace(',', '').replace('%', '').strip()
    try:
        return float(s)
    except:
        return 0.0

def fetch_bhavcopy_for_date(trade_date: datetime.date) -> pd.DataFrame:
    """Fetches NSE full bhavcopy for a specific date and returns filtered EQ DataFrame."""
    try:
        raw = jnse.full_bhavcopy_raw(trade_date)
        if not raw or "404 Not Found" in raw or len(raw) < 1000:
            return pd.DataFrame()
        
        df = pd.read_csv(io.StringIO(raw))
        df.columns = [c.strip() for c in df.columns]
        if 'SERIES' not in df.columns or 'SYMBOL' not in df.columns:
            return pd.DataFrame()
            
        eq_df = df[df['SERIES'].astype(str).str.strip() == 'EQ'].copy()
        return eq_df
    except Exception as e:
        # Weekend, market holiday, or unavailable date
        return pd.DataFrame()

def ingest_bhavcopy_df(df: pd.DataFrame, trade_date: datetime.date, db_path=None) -> int:
    """Inserts all equity rows from a Bhavcopy DataFrame into daily_market_quotes."""
    if df.empty:
        return 0
        
    date_str = trade_date.strftime('%Y-%m-%d')
    conn = get_connection(db_path) if db_path else get_connection()
    cur = conn.cursor()

    # Get known symbols in companies table to maintain relational integrity
    cur.execute("SELECT symbol FROM companies")
    known_symbols = set(r[0] for r in cur.fetchall())

    rows_to_insert = []
    for _, row in df.iterrows():
        sym = str(row['SYMBOL']).strip()
        if not sym:
            continue
            
        # Ensure company exists in master table
        if sym not in known_symbols:
            cur.execute("INSERT OR IGNORE INTO companies (symbol, name, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)", (sym, sym))
            known_symbols.add(sym)

        cmp_val = clean_val(row.get('CLOSE_PRICE'))
        open_val = clean_val(row.get('OPEN_PRICE'))
        high_val = clean_val(row.get('HIGH_PRICE'))
        low_val = clean_val(row.get('LOW_PRICE'))
        vol_val = int(clean_val(row.get('TTL_TRD_QNTY')))
        deliv_pct = clean_val(row.get('DELIV_PER'))
        turnover = clean_val(row.get('TURNOVER_LACS'))

        rows_to_insert.append((
            sym, date_str, cmp_val, open_val, high_val, low_val, vol_val, deliv_pct, turnover, 7.0
        ))

    cur.executemany('''
    INSERT OR REPLACE INTO daily_market_quotes (
        symbol, date, cmp, open_price, high_price, low_price, volume, delivery_pct, turnover_lacs, gsec_10y
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', rows_to_insert)

    conn.commit()
    conn.close()
    return len(rows_to_insert)

def sync_latest_bhavcopy(days_back=7, db_path=None) -> tuple[Optional[datetime.date], int]:
    """
    Finds the most recent trading day Bhavcopy (walking backwards from today) and ingests it.
    """
    today = datetime.date.today()
    for i in range(days_back):
        d = today - datetime.timedelta(days=i)
        if d.weekday() >= 5:  # Skip Saturday & Sunday
            continue
        df = fetch_bhavcopy_for_date(d)
        if not df.empty:
            count = ingest_bhavcopy_df(df, d, db_path)
            return d, count
            
    return None, 0

def backfill_bhavcopies(start_date: datetime.date, end_date: datetime.date, db_path=None):
    """Backfills daily bhavcopies for a given date range."""
    curr = start_date
    total_ingested = 0
    while curr <= end_date:
        if curr.weekday() < 5:
            df = fetch_bhavcopy_for_date(curr)
            if not df.empty:
                c = ingest_bhavcopy_df(df, curr, db_path)
                print(f"[{curr}] Ingested {c} stocks")
                total_ingested += c
            else:
                print(f"[{curr}] No Bhavcopy (Holiday/Weekend)")
        curr += datetime.timedelta(days=1)
    return total_ingested

if __name__ == "__main__":
    print("Fetching and ingesting latest NSE Bhavcopy...")
    latest_date, count = sync_latest_bhavcopy()
    if latest_date:
        print(f"Successfully synced Bhavcopy for {latest_date}: {count} stocks updated!")
    else:
        print("Could not find recent Bhavcopy.")
