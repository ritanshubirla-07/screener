import openpyxl
import sqlite3
import pandas as pd
import os

from .database import get_connection, DB_PATH

TEMPLATE_PATH = r"C:\Users\hiten\Downloads\JSW_Steel analysis.xlsx"

def generate_identical_excel(symbol, output_path=None, template_path=TEMPLATE_PATH, db_path=None):
    conn = get_connection(db_path) if db_path else get_connection()
    comp = conn.execute("SELECT * FROM companies WHERE symbol=?", (symbol,)).fetchone()
    if not comp:
        conn.close()
        raise ValueError(f"Company {symbol} not found in database. Ingest first.")
        
    df_ann = pd.read_sql("SELECT * FROM annual_financials WHERE symbol=? ORDER BY fiscal_year ASC", conn, params=(symbol,))
    df_q = pd.read_sql("SELECT * FROM quarterly_financials WHERE symbol=? ORDER BY period ASC", conn, params=(symbol,))
    mkt = conn.execute("SELECT * FROM daily_market_quotes WHERE symbol=? ORDER BY date DESC LIMIT 1", (symbol,)).fetchone()
    sh = conn.execute("SELECT * FROM shareholding WHERE symbol=? ORDER BY period DESC LIMIT 1", (symbol,)).fetchone()
    res = conn.execute("SELECT * FROM analysis_results WHERE symbol=?", (symbol,)).fetchone()
    conn.close()

    # Load master template with styling preserved
    wb_form = openpyxl.load_workbook(template_path, data_only=False)
    wb_val = openpyxl.load_workbook(template_path, data_only=True)

    ws = wb_form['VM Analysis']
    ws_val = wb_val['VM Analysis']

    # 1. Clean all [1] external formulas to their evaluated values
    for r in range(1, ws.max_row + 1):
        for c in range(1, ws.max_column + 1):
            f = ws.cell(r, c).value
            v = ws_val.cell(r, c).value
            if f and str(f).startswith('='):
                if '[1]' in str(f):
                    ws.cell(r, c).value = v

    # 2. Delete invalid external defined name
    if 'UPDATE' in wb_form.defined_names:
        del wb_form.defined_names['UPDATE']

    # 3. Clear external links relationship
    wb_form._external_links = []

    # 4. Update Banner Row 3
    ws.cell(3, 1, comp[1].upper()) # Company Name
    if res:
        ws.cell(3, 7, res[2]) # Quality score
        ws.cell(3, 8, res[3]) # Max quality
        ws.cell(3, 9, round(res[2] / res[3], 4) if res[3] else 0)
        ws.cell(3, 11, res[4]) # Valuation score
        ws.cell(3, 12, res[5]) # Max valuation
        ws.cell(3, 13, round(res[4] / res[5], 4) if res[5] else 0)

    # 5. Get 10 most recent fiscal years
    years_data = df_ann.tail(10).reset_index(drop=True)
    num_years = len(years_data)

    def populate_row(row_idx, val_list):
        for idx in range(num_years):
            col_idx = 5 + idx
            val = val_list[idx]
            ws.cell(row_idx, col_idx, val)

    # Populate Date Headers across all tables
    date_headers = [f"{row['fiscal_year']}-03-31" for _, row in years_data.iterrows()]
    date_rows = [18, 42, 66, 81, 97, 115, 127, 139, 152, 168, 183, 235, 274, 321, 346, 381]
    for r in date_rows:
        populate_row(r, date_headers)

    # 6. Populate Annual Financial Tables
    populate_row(19, years_data['sales'].tolist()) # Sales
    populate_row(43, years_data['sales'].tolist()) # Sales
    populate_row(44, years_data['operating_profit'].tolist()) # Op Profit
    populate_row(45, [(op/s if s>0 else 0) for op, s in zip(years_data['operating_profit'], years_data['sales'])]) # OPM
    populate_row(46, years_data['pat'].tolist()) # Net profit
    populate_row(47, [(p/s if s>0 else 0) for p, s in zip(years_data['pat'], years_data['sales'])]) # NPM

    # Tax Payout (Rows 67-69)
    populate_row(67, years_data['pbt'].tolist())
    populate_row(68, years_data['tax_amount'].tolist())
    populate_row(69, [t/100.0 for t in years_data['tax_pct'].tolist()])

    # Interest Coverage (Rows 82-84)
    ebit_list = (years_data['operating_profit'] - years_data['depreciation']).tolist()
    int_list = years_data['interest'].tolist()
    populate_row(82, ebit_list)
    populate_row(83, int_list)
    populate_row(84, [(e/i if i>0 else 99) for e, i in zip(ebit_list, int_list)])

    # Debt to Equity (Rows 98-100)
    populate_row(98, years_data['borrowings'].tolist())
    populate_row(99, years_data['equity_capital'].tolist())
    populate_row(100, years_data['reserves'].tolist())

    # Current Ratio (Rows 116-117)
    populate_row(116, years_data['other_assets'].tolist())
    populate_row(117, years_data['other_liabilities'].tolist())

    # Turnover Ratios
    populate_row(128, years_data['sales'].tolist())
    populate_row(129, years_data['fixed_assets'].tolist()) # Net Block
    populate_row(140, years_data['sales'].tolist())
    populate_row(141, years_data['inventory'].tolist())
    populate_row(153, years_data['sales'].tolist())
    populate_row(154, years_data['debtors'].tolist())

    # Cash Flows
    populate_row(169, years_data['cfo'].tolist())
    populate_row(184, years_data['cfo'].tolist())
    populate_row(185, years_data['pat'].tolist())

    populate_row(237, (years_data['pat'] - years_data['dividend_amount']).tolist()) # Retained Profits
    populate_row(275, years_data['pat'].tolist())
    populate_row(276, years_data['dividend_amount'].tolist())

    populate_row(322, [(p/s if s>0 else 0) for p, s in zip(years_data['pat'], years_data['sales'])])
    populate_row(323, years_data['sales'].tolist())
    populate_row(324, years_data['fixed_assets'].tolist())
    populate_row(326, years_data['depreciation'].tolist())
    populate_row(328, years_data['pat'].tolist())
    populate_row(329, years_data['dividend_amount'].tolist())

    populate_row(347, years_data['fixed_assets'].tolist())
    populate_row(348, years_data['cwip'].tolist())
    populate_row(349, years_data['depreciation'].tolist())
    populate_row(350, years_data['capex'].tolist())
    populate_row(351, years_data['cfo'].tolist())

    populate_row(382, years_data['pat'].tolist())

    # 7. Valuation Stats (Rows 372-379)
    if mkt:
        cmp_val = float(mkt['cmp'] or 0.0)
        mkt_cap = float(mkt['market_cap'] or 0.0)
        pe_val = float(mkt['pe'] or 0.0)
        bv_val = float(mkt['book_value'] or 0.0)
        div_y = float(mkt['div_yield'] or 0.0)
        gsec_10y = float(mkt['gsec_10y'] or 7.0)

        ws.cell(372, 4, cmp_val) # CMP
        ws.cell(372, 9, mkt_cap) # Market Cap
        ws.cell(374, 4, round(cmp_val / pe_val, 2) if pe_val else 0) # TTM EPS
        ws.cell(375, 4, bv_val) # Book value
        ws.cell(377, 4, gsec_10y / 100.0) # 10Y G-Sec
        ws.cell(378, 4, years_data['sales'].iloc[-1]) # TTM Sales
        ws.cell(379, 4, round(cmp_val * div_y / 100.0, 2)) # Div per share

    # 8. Populate Quarterly Sales (Rows 21-22, 50-54)
    if not df_q.empty:
        q_tail = df_q.tail(8).reset_index(drop=True)
        q_count = len(q_tail)
        for q_idx in range(q_count):
            c_idx = 5 + q_idx
            ws.cell(21, c_idx, q_tail['period'].iloc[q_idx])
            ws.cell(22, c_idx, q_tail['sales'].iloc[q_idx])
            ws.cell(49, c_idx, q_tail['period'].iloc[q_idx])
            ws.cell(50, c_idx, q_tail['sales'].iloc[q_idx])
            ws.cell(51, c_idx, q_tail['operating_profit'].iloc[q_idx])
            ws.cell(53, c_idx, q_tail['pat'].iloc[q_idx])

    if not output_path:
        output_path = f"{symbol}_VM_Identical.xlsx"

    wb_form.save(output_path)
    print(f"Successfully generated clean, self-contained workbook at: {output_path}")
    return output_path

if __name__ == "__main__":
    generate_identical_excel("JSWSTEEL", output_path=r"c:\Users\hiten\mf-feature\JSW_Steel_VM_Identical.xlsx")
    generate_identical_excel("TCS", output_path=r"c:\Users\hiten\mf-feature\TCS_VM_Identical.xlsx")
    generate_identical_excel("INFY", output_path=r"c:\Users\hiten\mf-feature\INFY_VM_Identical.xlsx")
