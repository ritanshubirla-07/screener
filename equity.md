# Equity Fundamental Analysis & Database Engine

A complete, self-contained system to fetch, store, and analyze 10+ years of fundamental financial data for any Indian equity (NSE/BSE) using the 34-parameter Dr. Vijay Malik (*"Peaceful Investing"*) framework, with automated daily market updates and identical Excel report generation.

---

## 1. Database Architecture & Layout (`equity_engine/equity_master.db`)

The database is an optimized SQLite relational database located at `equity_engine/equity_master.db`. It consists of **6 normalized tables**:

```
┌────────────────────────────────────────────────────────┐
│                   COMPANIES (Master)                   │
│   symbol (PK), name, industry, bse_code, is_financial  │
└───────────────────────────┬────────────────────────────┘
                            │
       ┌────────────────────┼────────────────────┬────────────────────┐
       ▼                    ▼                    ▼                    ▼
┌──────────────┐   ┌─────────────────┐   ┌───────────────┐   ┌────────────────┐
│ DAILY MARKET │   │ ANNUAL STATEMENTS│  │   QUARTERS    │   │  SHAREHOLDING  │
│    QUOTES    │   │  (10–12 Years)  │   │(8–13 Quarters)│   │  (Promoters,   │
│ (CMP, PE, MC)│   │(P&L, BS, CF, FCF│   │ (Sales, PAT)  │   │   FIIs, DIIs)  │
└──────────────┘   └─────────────────┘   └───────────────┘   └────────────────┘
                            │
                            ▼
              ┌───────────────────────────┐
              │     ANALYSIS_RESULTS      │
              │  (Quality Score, Valuation│
              │   Score, Total %, JSON)   │
              └───────────────────────────┘
```

### Table 1: `companies` (Master Table)
Stores identification and classification for each tracked stock.
* `symbol` (TEXT, PK): NSE ticker symbol (e.g. `'RELIANCE'`, `'TCS'`, `'JSWSTEEL'`).
* `name` (TEXT): Full registered company name.
* `industry` (TEXT): Industry / sector classification.
* `bse_code` (TEXT): 6-digit BSE scrip code (e.g. `'500228'`).
* `screener_url` (TEXT): Resolved public filing URL.
* `is_financial` (BOOLEAN): Flag (`1` for Banks/NBFCs, `0` for non-financial).
* `updated_at` (TIMESTAMP): Last sync timestamp.

### Table 2: `daily_market_quotes` (Market Pricing & Valuation)
Appended daily at market close.
* `symbol` (TEXT, PK)
* `date` (TEXT, PK): Trading date (`YYYY-MM-DD`).
* `cmp` (REAL): Current closing market price (₹).
* `market_cap` (REAL): Total market capitalization (₹ Cr).
* `pe` (REAL): Stock Price-to-Earnings ratio.
* `book_value` (REAL): Book value per share (₹).
* `div_yield` (REAL): Dividend yield (%).
* `roce` (REAL): Return on Capital Employed (%).
* `roe` (REAL): Return on Equity (%).
* `face_value` (REAL): Face value per share (₹).
* `gsec_10y` (REAL): 10-Year Government Securities benchmark rate (%).

### Table 3: `annual_financials` (10–12 Years Historical Statements)
Stores standardized annual balance sheet, P&L, and cash flows.
* `symbol` (TEXT, PK), `period` (TEXT, PK, e.g. `'Mar 2024'`), `fiscal_year` (INTEGER)
* **P&L:** `sales`, `expenses`, `operating_profit`, `opm_pct`, `other_income`, `interest`, `depreciation`, `pbt`, `tax_amount`, `tax_pct`, `pat`, `eps`.
* **Dividends:** `dividend_amount`, `div_payout_pct`.
* **Balance Sheet:** `equity_capital`, `reserves`, `net_worth`, `borrowings` (Total Debt), `other_liabilities`, `fixed_assets` (Net Block), `cwip`, `investments`, `other_assets`, `inventory`, `debtors`.
* **Cash Flows:** `cfo` (Cash from Operations), `capex`, `fcf` (Free Cash Flow = CFO - Capex).

### Table 4: `quarterly_financials` (Last 8–13 Quarters)
Tracks quarterly performance used for 4-quarter YoY momentum and consistency.
* `symbol` (TEXT, PK), `period` (TEXT, PK, e.g. `'Sep 2024'`), `quarter_date` (TEXT)
* `sales`, `expenses`, `operating_profit`, `opm_pct`, `other_income`, `interest`, `depreciation`, `pbt`, `tax_pct`, `pat`, `eps`.

### Table 5: `shareholding` (Ownership Structure)
Tracks institutional and promoter holding trends.
* `symbol` (TEXT, PK), `period` (TEXT, PK)
* `promoters_pct`, `fiis_pct`, `diis_pct`, `public_pct`, `others_pct`.

### Table 6: `analysis_results` (Vijay Malik Framework Scores)
Stores the evaluated 34-parameter scoring.
* `symbol` (TEXT, PK)
* `calculated_at` (TIMESTAMP)
* `quality_score` (REAL, out of 57/61)
* `max_quality` (REAL)
* `valuation_score` (REAL, out of 10)
* `max_valuation` (REAL)
* `total_score` (REAL)
* `max_total` (REAL)
* `score_pct` (REAL)
* `parameters_json` (TEXT): Full JSON structure containing all 34 parameters, individual scores, criteria, and calculated metrics.

---

## 2. What the 2 Scripts Do

### Script 1: `daily_append.py` (Daily Incremental Updater)

**Purpose:** Runs after market close (~4:00 PM) to update market quotes and re-evaluate scores across all tracked stocks.

#### Key Functions:
1. Reads all active stocks from the `companies` table.
2. Appends today's record into `daily_market_quotes` (closing price, market cap, PE, book value, dividend yield).
3. Detects newly announced quarterly/annual reports and inserts them into `quarterly_financials` and `annual_financials`.
4. Re-calculates all 34 parameters and updates `analysis_results`.
5. Logs all activity with timestamps to `daily_equity_update.log`.

#### Usage:
```bash
# High-speed daily update (Bhavcopy for all 2,000+ stocks + fundamentals for tracked universe):
python daily_append.py

# Update a single stock:
python daily_append.py --symbol TCS

# Update and automatically re-generate Excel files:
python daily_append.py --generate-excel

# Skip NSE Bhavcopy quote sync (only update fundamentals):
python daily_append.py --no-bhav
```

---

### Script 2: `generate_vm_excel.py` (Identical Excel Generator)

**Purpose:** Generates a standalone Excel workbook that is **100% identical** in layout, styles, fonts, borders, fills, and formulas to the original master template (`JSW_Steel analysis.xlsx`), populated with data for **any requested company**.

#### Key Features:
1. **Zero Excel Repair Warnings:** Automatically strips dead third-party external links (`_external_links` / `[1]`) and cleans dangling named ranges (`UPDATE`), so Excel opens the file immediately with zero repair popups.
2. **Preserves Complete 386-Row Structure:**
   * Row 1: "Shri Sai" header.
   * Row 3: Score summary banner (Quality Score, Valuation Score, Combined Score).
   * Rows 6–368: All 34 parameters, criteria descriptions, and actual scores.
   * Preserves all `"Dig Deep "` qualitative notes in Column L.
   * Embeds all 16 underlying multi-year data tables (Annual Sales, Quarterly Sales, P&L Margins, Debt/Equity, Asset/Inventory/Debtor Turnover, CFO vs PAT, SSGR, Free Cash Flow, and Valuation Stats).
3. **Instant On-Demand Ingestion:** If the requested stock is not yet in the local database, it fetches 10+ years of data, ingests it into SQLite, and generates the file in ~3 seconds.

#### Usage:
```bash
# Generate report for any stock:
python generate_vm_excel.py RELIANCE
python generate_vm_excel.py TCS
python generate_vm_excel.py JSWSTEEL
python generate_vm_excel.py INFY
python generate_vm_excel.py HDFCBANK

# Custom output filename:
python generate_vm_excel.py RELIANCE -o "Reliance_Analysis.xlsx"
```

---

## 3. High-Scale Market Pipeline & Bulk Ingestion

To compete with platforms like Screener and handle institutional-grade scale across the entire Indian stock market:

### 1. Master Universe (`equity_engine/universe.py`)
* Automatically seeds all **2,333 active NSE equities** (`SERIES == 'EQ'`) from official NSE master listings.
* Categorizes by index: **Nifty 50**, **Nifty 500**, and **All Listed Equities**.

### 2. High-Speed NSE Bhavcopy Engine (`equity_engine/bhavcopy.py`)
* Downloads official daily NSE Bhavcopy archives in **1 single HTTP request (<2 seconds)**.
* Bulk-updates closing price (CMP), open, high, low, volume, turnover, and delivery percentage across **all 2,600+ securities simultaneously**.
* Supports historical backfilling for any date range.

### 3. Resumable Bulk Fundamental Ingester (`equity_engine/bulk_ingester.py`)
* Systematically pre-loads 10–12 years of fundamental statements, quarterly reports, and 34-parameter Vijay Malik scores:
```bash
# Ingest the Nifty 50 (Mega-caps, ~45 seconds):
python -m equity_engine.bulk_ingester --tier nifty50

# Ingest the Nifty 500 (94% of Indian market cap, ~6-8 minutes):
python -m equity_engine.bulk_ingester --tier nifty500

# Ingest the entire NSE listed universe (~2,000+ stocks):
python -m equity_engine.bulk_ingester --tier all

# Custom symbols:
python -m equity_engine.bulk_ingester --symbols TATAMOTORS,ITC,LT
```
* **Resumable & Safe**: Skips already synced companies, features 0.8s rate-limiting with random jitter to prevent IP throttle, and saves progress dynamically after every company.
