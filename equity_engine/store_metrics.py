import sqlite3
import os
import sys
import time

def calculate_and_store_all_metrics(db_path=None):
    if not db_path:
        db_path = os.path.join(os.path.dirname(__file__), "equity_master.db")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    print("=" * 70)
    print("  CALCULATING & STORING RAW-AUDITED VALUATION & QUALITY METRICS")
    print("=" * 70)

    # 1. Fetch latest annual financials for every company
    c.execute("""
        SELECT 
            af.symbol,
            af.period,
            af.fiscal_year,
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
    annual_data = {row['symbol']: dict(row) for row in c.fetchall()}
    print(f"Loaded latest audited annual financials for {len(annual_data):,} companies.")

    # 2. Fetch all daily market quotes
    c.execute("SELECT symbol, date, cmp, face_value FROM daily_market_quotes WHERE cmp > 0")
    quotes = c.fetchall()
    print(f"Loaded {len(quotes):,} market quotes to calculate metrics for.")

    updates = []
    skipped = 0

    for q in quotes:
        sym = q['symbol']
        cmp = float(q['cmp'] or 0.0)
        fv = float(q['face_value'] or 10.0)
        af = annual_data.get(sym)

        if not af or cmp <= 0:
            skipped += 1
            continue

        pat = float(af['pat'] or 0.0)
        eps = float(af['eps'] or 0.0)
        sales = float(af['sales'] or 0.0)
        op = float(af['operating_profit'] or 0.0)
        depn = float(af['depreciation'] or 0.0)
        nw = float(af['net_worth'] or 0.0)
        debt = float(af['borrowings'] or 0.0)
        div = float(af['dividend_amount'] or 0.0)
        eq_cap = float(af['equity_capital'] or 0.0)

        # Shares outstanding (in Cr shares)
        if eps > 0 and pat > 0:
            shares_cr = pat / eps
        elif eq_cap > 0 and fv > 0:
            shares_cr = eq_cap / fv
        else:
            shares_cr = 0.0

        # Market Cap (in Rs Cr)
        mcap = round(cmp * shares_cr, 2) if shares_cr > 0 else None

        # P/E Ratio
        pe = round(cmp / eps, 2) if eps > 0 else None

        # Book Value per share & P/B
        bv = round(nw / shares_cr, 2) if (shares_cr > 0 and nw > 0) else None

        # Capital Employed & ROCE
        ebit = op - depn
        cap_emp = nw + debt
        roce = round((ebit / cap_emp) * 100.0, 2) if cap_emp > 0 else None

        # ROE
        roe = round((pat / nw) * 100.0, 2) if nw > 0 else None

        # Dividend Yield %
        div_yield = round((div / mcap) * 100.0, 4) if (mcap and mcap > 0 and div > 0) else 0.0

        updates.append((mcap, pe, bv, roce, roe, div_yield, sym, q['date']))

    # Batch update daily_market_quotes
    c.executemany("""
        UPDATE daily_market_quotes
        SET 
            market_cap = ?,
            pe = ?,
            book_value = ?,
            roce = ?,
            roe = ?,
            div_yield = ?
        WHERE symbol = ? AND date = ?
    """, updates)

    conn.commit()
    print(f"Successfully calculated & stored metrics for {len(updates):,} companies in daily_market_quotes!")
    print(f"Skipped without annual statements: {skipped:,}")

    # Spot check sample
    print("\n--- SAMPLE CALCULATED & STORED RECORDS ---")
    c.execute("""
        SELECT symbol, cmp, market_cap, pe, book_value, roce, roe, div_yield 
        FROM daily_market_quotes 
        WHERE symbol IN ('FRONTSP', 'TCS', 'RELIANCE', 'ABB', 'BAJFINANCE', 'APOLLOTYRE')
    """)
    for r in c.fetchall():
        print(f"  {r['symbol']:<12}: CMP=Rs.{r['cmp']:>7.1f} | MCap=Rs.{str(r['market_cap']):>10} Cr | PE={str(r['pe']):>6}x | BV=Rs.{str(r['book_value']):>7} | ROCE={str(r['roce']):>5}% | ROE={str(r['roe']):>5}% | DivY={str(r['div_yield']):>6}%")

    conn.close()

if __name__ == "__main__":
    calculate_and_store_all_metrics()
