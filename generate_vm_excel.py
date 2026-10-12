import sys
import os
import argparse
from equity_engine.fetcher import fetch_and_store_company
from equity_engine.calculator import run_vm_analysis
from equity_engine.template_generator import generate_identical_excel
from equity_engine.database import get_connection, DB_PATH

def main():
    parser = argparse.ArgumentParser(description="Generate 100% Identical Vijay Malik Stock Analysis Excel Report")
    parser.add_argument("symbol", help="Stock ticker symbol (e.g. JSWSTEEL, TCS, RELIANCE, INFY)")
    parser.add_argument("--output", "-o", help="Output Excel file path (default: <SYMBOL>_VM_Identical.xlsx)", default=None)
    args = parser.parse_args()

    sym = args.symbol.upper().strip()
    out_file = args.output if args.output else f"{sym}_VM_Identical.xlsx"
    out_path = os.path.abspath(out_file)

    print(f"\n=======================================================")
    print(f"  IDENTICAL EXCEL GENERATOR: {sym}")
    print(f"=======================================================")

    # Check if symbol exists in database, if not fetch first
    conn = get_connection()
    row = conn.execute("SELECT symbol, name FROM companies WHERE symbol=?", (sym,)).fetchone()
    conn.close()

    if not row:
        print(f"[1/3] '{sym}' not in local database. Fetching 10+ years data...")
        comp_info = fetch_and_store_company(sym)
        sym = comp_info['symbol']
        print(f"      Ingested: {comp_info['name']} ({sym})")
    else:
        print(f"[1/3] Found '{row['name']}' ({sym}) in database.")

    # Re-calculate 34-parameter analysis
    print(f"[2/3] Calculating 34 parameters and scores...")
    analysis = run_vm_analysis(sym)
    print(f"      Quality: {analysis['quality_score']}/{analysis['max_quality']} | Valuation: {analysis['valuation_score']}/{analysis['max_valuation']}")

    # Generate identical Excel workbook
    print(f"[3/3] Generating identical Excel workbook preserving all original styles...")
    generate_identical_excel(sym, output_path=out_path)
    print(f"\nSUCCESS! File generated at:\n  {out_path}\n")

if __name__ == "__main__":
    main()
