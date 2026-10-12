"""
run_daily_equity_pipeline.py
============================
Unified, fully-automated master execution pipeline for the Indian Equity Master Database.

WHAT IT HANDLES AUTOMATICALLY:
1. Daily Market Intake (Every Trading Day | ~5 to 10 seconds):
   - Ingests latest official NSE Bhavcopy for today's market session (and catches up on any missed days).
   - Ingests full OHLCV candles, volume, delivery % across all ~2,680 listed equities.
   - Automatically computes & updates today's live valuation ratios (P/E, P/B, Market Cap) with new closing prices.

2. Quarterly Financial Intake (Automated Periodic Sweep | ~85 minutes):
   - Tracks `last_financial_sync` timestamp in the database (`sync_state` table).
   - If 90+ days (1 full quarter) have elapsed since the last financial sync (or if --force-financials is passed):
     - Automatically runs the full quarterly ingestion of all active companies (~85 mins).
     - Scrapes new audited annual P&L, Balance Sheets, Cash Flows, Quarterly Results, and Shareholding.
     - Recalculates Dr. Vijay Malik 34-parameter quality and valuation scores.
     - Records the new sync timestamp in `sync_state`.
   - If not due yet:
     - Logs the days remaining until next quarterly intake and exits cleanly in seconds.

USAGE:
  python run_daily_equity_pipeline.py                    # Normal daily run (fast, auto-runs quarterly if due)
  python run_daily_equity_pipeline.py --force-financials # Force full ~85m quarterly financial sync right now
  python run_daily_equity_pipeline.py --no-bhav          # Skip market prices, check financials only
"""

import os
import sys
import argparse
import datetime
import time

# Ensure equity_engine is discoverable
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from equity_engine.database import get_connection, init_db, DB_PATH
from equity_engine.bhavcopy import sync_latest_bhavcopy, fetch_bhavcopy_for_date, ingest_bhavcopy_df
from equity_engine.bulk_ingester import bulk_ingest

LOG_FILE = os.path.join(BASE_DIR, "daily_equity_pipeline.log")

def log(msg: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{ts}] {msg}"
    print(formatted, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(formatted + "\n")
    except Exception:
        pass


def update_valuation_ratios_for_date(date_str: str, conn=None) -> int:
    """
    Calculates and populates Market Cap, P/E, Book Value, ROCE, ROE, and Div Yield
    for all quotes on a given date using raw audited annual figures.
    Appends full derived fundamental metrics in < 1 second.
    """
    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True

    c = conn.cursor()

    # 1. Fetch latest annual financials for every company
    c.execute("""
        SELECT 
            af.symbol,
            af.sales,
            af.operating_profit,
            af.depreciation,
            af.pat,
            af.eps,
            af.net_worth,
            af.borrowings,
            af.dividend_amount,
            af.equity_capital
        FROM annual_financials af
        INNER JOIN (
            SELECT symbol, max(fiscal_year) as max_fy
            FROM annual_financials
            GROUP BY symbol
        ) latest ON af.symbol = latest.symbol AND af.fiscal_year = latest.max_fy
    """)
    annual_data = {row[0]: {
        'sales': row[1], 'op': row[2], 'depn': row[3], 'pat': row[4],
        'eps': row[5], 'nw': row[6], 'debt': row[7], 'div': row[8], 'eq_cap': row[9]
    } for row in c.fetchall()}

    # 2. Fetch quotes for this date
    c.execute("SELECT symbol, cmp, face_value FROM daily_market_quotes WHERE date = ? AND cmp > 0", (date_str,))
    quotes = c.fetchall()

    updates = []
    for sym, cmp, fv in quotes:
        af = annual_data.get(sym)
        if not af or not cmp or cmp <= 0:
            continue

        pat = float(af['pat'] or 0.0)
        eps = float(af['eps'] or 0.0)
        op = float(af['op'] or 0.0)
        depn = float(af['depn'] or 0.0)
        nw = float(af['nw'] or 0.0)
        debt = float(af['debt'] or 0.0)
        div = float(af['div'] or 0.0)
        eq_cap = float(af['eq_cap'] or 0.0)
        face_val = float(fv or 10.0)

        # Shares outstanding (in Cr shares)
        if eps > 0 and pat > 0:
            shares_cr = pat / eps
        elif eq_cap > 0 and face_val > 0:
            shares_cr = eq_cap / face_val
        else:
            shares_cr = 0.0

        # Derived calculations
        mcap = round(cmp * shares_cr, 2) if shares_cr > 0 else None
        pe = round(cmp / eps, 2) if eps > 0 else None
        bv = round(nw / shares_cr, 2) if (shares_cr > 0 and nw > 0) else None
        ebit = op - depn
        cap_emp = nw + debt
        roce = round((ebit / cap_emp) * 100.0, 2) if cap_emp > 0 else None
        roe = round((pat / nw) * 100.0, 2) if nw > 0 else None
        div_yield = round((div / mcap) * 100.0, 4) if (mcap and mcap > 0 and div > 0) else 0.0

        updates.append((mcap, pe, bv, roce, roe, div_yield, sym, date_str))

    c.executemany("""
        UPDATE daily_market_quotes
        SET market_cap = ?, pe = ?, book_value = ?, roce = ?, roe = ?, div_yield = ?
        WHERE symbol = ? AND date = ?
    """, updates)

    conn.commit()
    if should_close:
        conn.close()
    return len(updates)


def run_daily_market_sync(days_back: int = 7) -> int:
    """
    Fetches the latest trading day closing prices from official NSE Bhavcopy,
    calculates derived metrics (P/E, P/B, MCap, ROCE, ROE, Div Yield),
    and APPENDS the quote record to build clean historical closing series.
    """
    log("==================================================================")
    log("PHASE 1: DAILY MARKET PRICES & VALUATIONS APPEND (NSE)")
    log("==================================================================")

    bhav_date, count = sync_latest_bhavcopy(days_back=days_back)
    if not bhav_date or count == 0:
        log("  * Market closed / Bhavcopy already up-to-date.")
        return 0

    d_str = bhav_date.strftime('%Y-%m-%d')
    log(f"  * Latest Trading Session: {d_str} ({count:,} stocks synced)")

    # Calculate and store all derived valuation and quality metrics for today's close
    conn = get_connection()
    val_count = update_valuation_ratios_for_date(d_str, conn=conn)
    conn.close()

    log(f"  * Appended daily close & calculated metrics (P/E, P/B, MCap, ROCE, ROE) for {val_count:,} stocks.")
    log(f"Phase 1 Complete: Market quotes appended successfully for {d_str}.")
    return count


def get_last_financial_sync_info() -> tuple[datetime.datetime, int]:
    """Retrieves last financial sync timestamp and days elapsed from sync_state table."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT value FROM sync_state WHERE key = 'last_financial_sync'")
    row = cur.fetchone()
    conn.close()

    default_dt = datetime.datetime(2026, 10, 10, 21, 17, 17)
    if not row or not row[0]:
        last_dt = default_dt
    else:
        try:
            last_dt = datetime.datetime.strptime(row[0].strip(), "%Y-%m-%d %H:%M:%S")
        except Exception:
            last_dt = default_dt

    elapsed_days = (datetime.datetime.now() - last_dt).days
    return last_dt, elapsed_days


def run_quarterly_financial_intake(force: bool = False, delay: float = 0.6, quarterly_threshold_days: int = 90) -> bool:
    """
    Checks if a quarterly financial sweep is due.
    If due (or forced), executes the full ~85m financial ingestion across active companies.
    """
    log("==================================================================")
    log("PHASE 2: QUARTERLY FINANCIALS & EARNINGS INTAKE CHECK")
    log("==================================================================")

    last_dt, elapsed_days = get_last_financial_sync_info()
    last_str = last_dt.strftime("%Y-%m-%d %H:%M:%S")

    log(f"Last Quarterly Financial Intake: {last_str} ({elapsed_days} days ago)")
    log(f"Quarterly Threshold: Every {quarterly_threshold_days} days (1 full quarter)")

    if not force and elapsed_days < quarterly_threshold_days:
        days_left = quarterly_threshold_days - elapsed_days
        log(f"  -> Financials are UP-TO-DATE. Next intake scheduled in {days_left} days.")
        log("  -> Skipping full financial crawl today.")
        return False

    # Intake is due or forced
    reason = "MANUAL OVERRIDE (--force-financials)" if force else f"THRESHOLD REACHED ({elapsed_days} >= {quarterly_threshold_days} days)"
    log("------------------------------------------------------------------")
    log(f"  >>> TRIGGERING FULL QUARTERLY FINANCIAL INTAKE: {reason} <<<")
    log("  >>> This will scrape updated P&L, Balance Sheets & Quarters from Screener.")
    log("  >>> Estimated duration: ~85 minutes. Running in auto-recovery mode...")
    log("------------------------------------------------------------------")

    conn = get_connection()
    raw_symbols = [r[0] for r in conn.execute("SELECT symbol FROM companies").fetchall()]
    conn.close()

    # Prioritize NSE named tickers first, then BSE numeric scrips
    nse_syms = sorted([s for s in raw_symbols if not (len(s) == 6 and s.isdigit())])
    bse_syms = sorted([s for s in raw_symbols if (len(s) == 6 and s.isdigit())])
    ordered_symbols = nse_syms + bse_syms

    # Execute bulk intake with refresh_active=True (skips confirmed dead/delisted ones, refreshes active ones)
    start_t = time.time()
    bulk_ingest(ordered_symbols, delay=delay, refresh_active=True)
    duration = time.time() - start_t

    # Record completion in sync_state
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    conn.execute("""
        INSERT OR REPLACE INTO sync_state (key, value, updated_at) 
        VALUES ('last_financial_sync', ?, CURRENT_TIMESTAMP)
    """, (now_str,))
    conn.commit()
    conn.close()

    log(f"  >>> QUARTERLY INTAKE COMPLETED in {int(duration // 60)}m {int(duration % 60)}s!")
    log(f"  >>> Timestamp updated to {now_str}.")
    return True


def main():
    parser = argparse.ArgumentParser(description="Master Daily & Quarterly Indian Equity Pipeline")
    parser.add_argument("--force-financials", action="store_true", help="Force full ~85m quarterly financial crawl right now")
    parser.add_argument("--no-bhav", action="store_true", help="Skip daily Bhavcopy price sync")
    parser.add_argument("--quarterly-days", type=int, default=90, help="Days interval between full financial crawls (default: 90 / 1 quarter)")
    parser.add_argument("--delay", type=float, default=0.6, help="Request delay in seconds during financial crawl (default: 0.6)")
    args = parser.parse_args()

    init_db(DB_PATH)
    start_time = datetime.datetime.now()

    log("##################################################################")
    log(f"INDIAN EQUITY MASTER AUTOMATION PIPELINE STARTED AT {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    log("##################################################################")

    # Step 1: Daily Market Sync
    if not args.no_bhav:
        try:
            run_daily_market_sync(days_back=5)
        except Exception as e:
            log(f"[ERROR] Daily Market Sync failed: {e}")

    # Step 2: Quarterly Financials Intake (Runs only when due or forced)
    try:
        run_quarterly_financial_intake(
            force=args.force_financials,
            delay=args.delay,
            quarterly_threshold_days=args.quarterly_days
        )
    except Exception as e:
        log(f"[ERROR] Quarterly Financial Intake failed: {e}")

    total_time = (datetime.datetime.now() - start_time).total_seconds()
    log("##################################################################")
    log(f"EQUITY PIPELINE FINISHED in {total_time:.2f}s ({int(total_time // 60)}m {int(total_time % 60)}s)")
    log("##################################################################\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
