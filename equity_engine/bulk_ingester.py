import argparse
import os
import sys
import time
import random
from datetime import datetime
from typing import List

from .database import get_connection, init_db
from .fetcher import fetch_and_store_company
from .calculator import run_vm_analysis
from .universe import get_nifty50_symbols, get_nifty500_symbols, populate_universe_master

LIVE_LOG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "crawl_live.log")

def live_log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{ts}] {msg}"
    print(formatted, flush=True)
    try:
        with open(LIVE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(formatted + "\n")
    except Exception:
        pass

def wait_for_connectivity():
    live_log(">>> [PAUSED] Screener BunnyCDN rate-limit / connection drop detected (WinError 10054).")
    live_log(">>> Auto-probing connection every 15s... (Tip: Switching or reconnecting VPN unblocks instantly!)")
    while True:
        time.sleep(15)
        try:
            import urllib.request
            req = urllib.request.Request("https://www.screener.in/api/company/search/?q=RELIANCE", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    live_log(">>> [RESTORED] Connection to Screener re-established with HTTP 200 OK! Resuming...")
                    time.sleep(2)
                    return
        except Exception:
            pass

def get_already_ingested_symbols(conn, refresh_active: bool = False) -> set:
    """Returns set of symbols to skip. If refresh_active=True, skips only confirmed dead/unlisted symbols."""
    cur = conn.cursor()
    cur.execute('''
    CREATE TABLE IF NOT EXISTS ingestion_log (
        symbol TEXT PRIMARY KEY,
        status TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    ''')
    if refresh_active:
        cur.execute("SELECT symbol FROM ingestion_log WHERE status IN ('no_data', 'unlisted')")
        return set(r[0] for r in cur.fetchall())
    else:
        cur.execute("SELECT DISTINCT symbol FROM annual_financials GROUP BY symbol HAVING count(*) >= 5")
        done = set(r[0] for r in cur.fetchall())
        cur.execute("SELECT symbol FROM ingestion_log WHERE status IN ('success', 'no_data', 'unlisted')")
        done.update(r[0] for r in cur.fetchall())
        return done

def log_symbol_status(sym: str, status: str, db_path=None):
    try:
        conn = get_connection(db_path)
        conn.execute("INSERT OR REPLACE INTO ingestion_log (symbol, status, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)", (sym, status))
        conn.commit()
        conn.close()
    except Exception:
        pass

def bulk_ingest(symbols: List[str], delay: float = 0.6, force: bool = False, refresh_active: bool = False, max_count: int = None):
    init_db()
    conn = get_connection()
    already_done = set() if force else get_already_ingested_symbols(conn, refresh_active=refresh_active)
    conn.close()

    target_list = [s.strip().upper() for s in symbols if s.strip()]
    if not force:
        to_process = [s for s in target_list if s not in already_done]
    else:
        to_process = target_list

    if max_count and max_count > 0:
        to_process = to_process[:max_count]

    total = len(to_process)
    skipped = len(target_list) - total
    live_log("=" * 65)
    live_log("  BULK EQUITY INGESTER & VALUATION ENGINE (REAL-TIME LOGGING)")
    live_log("=" * 65)
    live_log(f"Total Target Stocks: {len(target_list)}")
    live_log(f"Already Synced / Verified Dead (Skipped): {skipped}")
    live_log(f"To Process Now: {total}")
    live_log(f"Delay Between Requests: {delay}s (+ jitter)")
    live_log("=" * 65)

    if total == 0:
        live_log("All target stocks are already up to date in the database!")
        return

    success_count = 0
    fail_count = 0
    consecutive_network_errors = 0
    recent_durations = []
    start_time = time.time()

    for idx, sym in enumerate(to_process, 1):
        item_start = time.time()
        attempts = 0
        while attempts < 3:
            try:
                # 1. Fetch & Store
                info = fetch_and_store_company(sym)
                consecutive_network_errors = 0
                log_symbol_status(sym, "success")
                
                # 2. Run 34-parameter analysis
                analysis = run_vm_analysis(sym)
                q_score = analysis.get('quality_score', 0)
                max_q = analysis.get('max_quality', 57)
                v_score = analysis.get('valuation_score', 0)
                max_v = analysis.get('max_valuation', 10)
                tot_pct = analysis.get('score_pct', 0)

                success_count += 1
                item_duration = time.time() - item_start
                recent_durations.append(item_duration)
                if len(recent_durations) > 25:
                    recent_durations.pop(0)
                avg_sec = sum(recent_durations) / len(recent_durations)
                remaining = total - idx
                eta_sec = remaining * avg_sec
                eta_str = f"{int(eta_sec // 60)}m {int(eta_sec % 60)}s"

                live_log(f"[{idx:3d}/{total:3d}] {sym:<12} | {info['name'][:24]:<24} | Q: {q_score:4.1f}/{max_q:.0f} | V: {v_score:3.1f}/{max_v:.0f} ({tot_pct:4.1f}%) | ETA: {eta_str}")
                break

            except Exception as e:
                err_msg = str(e)
                
                # If confirmed unlisted/no filings on Screener, mark permanently so it is never retried
                if "could not find company" in err_msg.lower() or "not found" in err_msg.lower():
                    fail_count += 1
                    log_symbol_status(sym, "no_data")
                    live_log(f"[{idx:3d}/{total:3d}] {sym:<12} | [SKIP] No financial filings on exchange.")
                    break

                # Network block / reset self-healing handler
                if any(w in err_msg.lower() for w in ['timeout', 'connection', '429', 'retry', 'reset', '10054']):
                    consecutive_network_errors += 1
                    if consecutive_network_errors >= 2:
                        wait_for_connectivity()
                        consecutive_network_errors = 0
                        # Auto-retry this exact same stock without incrementing attempts!
                        continue
                    else:
                        live_log(f"       >>> Transient network reset on {sym}. Backing off for 12s...")
                        time.sleep(12.0)
                        attempts += 1
                        continue
                else:
                    fail_count += 1
                    live_log(f"[{idx:3d}/{total:3d}] {sym:<12} | ERROR: {err_msg[:60]}")
                    break

        # Batch rest every 150 companies to keep CDN bucket cold
        if idx % 150 == 0 and idx < total:
            live_log(f"--- Batch milestone ({idx}/{total}). Taking 30s health rest for CDN... ---")
            time.sleep(30.0)

        # Rate-limiting delay with light jitter
        if idx < total:
            sleep_time = delay + random.uniform(0.1, 0.25)
            time.sleep(sleep_time)

    total_time = time.time() - start_time
    live_log("=" * 65)
    live_log(f"  BULK INGESTION COMPLETED in {int(total_time // 60)}m {int(total_time % 60)}s")
    live_log(f"  Success: {success_count} | Skipped/No-Data: {fail_count} | Total Pre-Synced: {skipped}")
    live_log("=" * 65)

def main():
    parser = argparse.ArgumentParser(description="Bulk Ingest Financial Statements for Indian Equities")
    parser.add_argument("--tier", choices=["nifty50", "nifty500", "all"], default="nifty50",
                        help="Universe tier to ingest (default: nifty50)")
    parser.add_argument("--symbols", type=str, help="Comma-separated custom symbols (e.g. RELIANCE,TCS,INFY)")
    parser.add_argument("--limit", type=int, default=None, help="Max number of companies to ingest")
    parser.add_argument("--delay", type=float, default=0.6, help="Delay in seconds between requests (default: 0.6)")
    parser.add_argument("--force", action="store_true", help="Re-fetch even if already present in database")
    args = parser.parse_args()

    # Ensure companies master is seeded
    populate_universe_master()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.tier == "nifty50":
        symbols = get_nifty50_symbols()
    elif args.tier == "nifty500":
        symbols = get_nifty500_symbols()
    elif args.tier == "all":
        conn = get_connection()
        raw_symbols = [r[0] for r in conn.execute("SELECT symbol FROM companies").fetchall()]
        conn.close()
        # Prioritize NSE and named tickers first, followed by 6-digit BSE scrips
        nse_syms = sorted([s for s in raw_symbols if not (len(s) == 6 and s.isdigit())])
        bse_syms = sorted([s for s in raw_symbols if (len(s) == 6 and s.isdigit())])
        symbols = nse_syms + bse_syms

    bulk_ingest(symbols, delay=args.delay, force=args.force, max_count=args.limit)

if __name__ == "__main__":
    main()
