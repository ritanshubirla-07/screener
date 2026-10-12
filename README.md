# VIKABH EQUITY TERMINAL & RESEARCH ENGINE

An institutional-grade equity research terminal, fundamental valuation engine, and automated financial analysis platform for Indian equities (NSE & BSE).

Powered by a proprietary **51-Point Evaluation Model**, 20+ years of corporate-action adjusted historical price and volume data, dynamic company business profiles, and daily Bhavcopy syncing.

---

## Architecture Overview

```
screener/
├── equity_server.py                # Fast multi-threaded Python server & API proxy
├── equity_viewer.html              # High-density VIKABH terminal UI (Single-Page App)
├── equity.md                       # Methodology documentation for 51-Point Evaluation
├── requirements.txt                # Python package dependencies
├── generate_vm_excel.py            # CLI tool to export 51-point analysis to formatted Excel
├── run_daily_equity_pipeline.py    # Daily automated synchronization pipeline
├── run_daily_equity_pipeline.bat   # Windows Batch runner for Task Scheduler
├── setup_equity_daily_cron.ps1     # PowerShell script to register daily automated cron
│
└── equity_engine/                  # Core Python analytics & ingestion package
    ├── database.py                 # SQLite connection manager & schema definition
    ├── calculator.py               # 51-point evaluation engine across 5 pillars
    ├── fetcher.py                  # Screener parser (fundamentals, P&L, BS, Cash Flow)
    ├── daily_history.py            # Historical quote manager
    ├── bhavcopy.py                 # NSE daily Bhavcopy downloader and quote updater
    ├── bse_bhavcopy.py             # BSE Bhavcopy downloader and sync
    ├── universe.py                 # Active stock universe tracking & ticker resolver
    ├── bulk_ingester.py            # Bulk screener scraper and database populator
    ├── rescore_all.py              # Batch recalculation for all tracked equities
    ├── excel_exporter.py           # Institutional Excel report generator
    ├── deduplicate.py              # Scrip deduplication & identifier normalizer
    ├── sector_reclassify.py        # Industry and sector taxonomies
    └── cache/
        ├── bse_scrips.json         # BSE scrip code to ticker mapping
        └── EQUITY_L.csv            # NSE active equity master list
```

---

## Quick Start Setup Guide

Follow these steps to set up and run the terminal on your local machine.

### 1. Prerequisites
- **Python 3.10+** installed on your system.
- Git installed.

### 2. Clone the Repository
```bash
git clone https://github.com/ritanshubirla-07/screener.git
cd screener
```

### 3. Create a Virtual Environment & Install Dependencies
**Windows (PowerShell / CMD):**
```powershell
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

**macOS / Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## 4. Database Setup (Crucial Step)

The master database (`equity_master.db`) contains pre-ingested financial statements (Annual P&L, Balance Sheet, Cash Flow), quarterly results, shareholding patterns, daily market quotes, and computed 51-point scores. Due to GitHub file limits, it is hosted on Google Drive.

1. **Download `equity_master.db`** from the Google Drive link provided by your team.
2. **Place the file inside the `equity_engine/` directory**:
   ```
   screener/
   └── equity_engine/
       └── equity_master.db   <--- Place the downloaded file here
   ```
   *Expected file location:* `equity_engine/equity_master.db`

> **Note:** If starting with a fresh database without pre-existing data, run:
> ```bash
> python -c "from equity_engine.database import init_db; init_db()"
> ```

---

## 5. Running the Terminal

Start the local server:
```bash
python equity_server.py
```

Once running, open your browser and navigate to:
```
http://localhost:8050/?symbol=FRONTSP
```
Or explore any other stock directly:
- `http://localhost:8050/?symbol=TCS`
- `http://localhost:8050/?symbol=RELIANCE`
- `http://localhost:8050/?symbol=GANDHITUBE`

---

## Features & Capabilities

### 1. High-Density Terminal Interface (`equity_viewer.html`)
- **Branding**: Clean, institutional VIKABH EQUITY TERMINAL typography (Roboto Condensed bold/uppercase).
- **9-Box Metrics Ribbon**: Instant visibility into `MKT CAP`, `PRICE`, `ROCE %`, `ROE %`, `P/E RATIO`, `P/B RATIO`, `DIV YIELD`, `DEBT/EQUITY`, and `52W RANGE`.
- **Typo-Tolerant Search Bar**: Fast multi-strategy company search supporting exact tickers, BSE scrip codes, company name fragments, and typo tolerance (e.g., `gandhi tube` resolves to `GANDHITUBE`).

### 2. Proprietary 51-Point Institutional Scorecard
- Evaluates companies across **5 Fundamental Pillars** with strictly **51.0 maximum points**:
  1. **Growth (13.0 pts)**: Sales CAGR (3Y/5Y/10Y), Profit CAGR (3Y/5Y/10Y).
  2. **Profitability & Quality (19.0 pts)**: ROCE 3Y Avg & Trend, ROE 3Y Avg & Trend, Operating Margin consistency, Asset Turnover, Working Capital Days, Cash Conversion Cycle.
  3. **Cash Flow & Capital Efficiency (7.0 pts)**: Cumulative CFO vs PAT (5Y), Free Cash Flow consistency, Capital Allocation.
  4. **Leverage & Solvency (6.0 pts)**: Debt-to-Equity Ratio, Interest Coverage Ratio (ICR).
  5. **Valuation & Margin of Safety (6.0 pts)**: P/E vs Historical/Industry, Earnings Yield vs 10Y G-Sec.
- **Interactive Radar Chart**: HTML5 Canvas visualization animating company performance across all five pillars in real-time.

### 3. 20+ Year Interactive Chart Engine
- Directly streams corporate-action adjusted historical price, volume, 50 DMA, and 200 DMA from Screener API.
- Switch between **1Y**, **3Y**, **5Y**, **10Y**, and **MAX (20+ Years back to 2005)**.
- High-precision crosshair with date, price, volume, and moving average tooltips.
- **Automated Performance Analytics**: Calculates and displays `START PRICE`, `END PRICE`, `PERIOD LOW`, `PERIOD HIGH`, `TOTAL RETURN %`, and `CAGR %`.

### 4. Dynamic Company Profiles & Revenue Breakups
- On-demand streaming from Screener without bloating local database storage.
- Auto-extracts clean company profile overviews and formats commentary into structured **Segment / Revenue Breakup Tables** (e.g., BFSI, Healthcare, Regional segments).

### 5. Complete Financial Statements & Shareholding
- **Annual P&L**: Multi-year revenue, operating profit, OPM %, net profit, EPS, and dividend payouts.
- **Balance Sheet**: Net worth, reserves, total debt/borrowings, fixed assets, and CWIP.
- **Cash Flow**: Cash from Operations (CFO), Cash from Investing (CFI), Cash from Financing (CFF), and Free Cash Flow (FCF).
- **Shareholding**: Promoter holding, FII, DII, Public, and Pledged share trends.

---

## Automated Daily Pipelines

To keep prices and daily trading metrics up to date:

### Daily Bhavcopy & Screener Sync
Run the daily automated pipeline:
```bash
python run_daily_equity_pipeline.py
```
This automatically:
1. Downloads the latest official Bhavcopy from NSE and BSE.
2. Updates daily closing price, volume, delivery percentage, and turnover in `equity_master.db`.
3. Scrapes updated quarterly or annual filings if new results are declared.
4. Recalculates the 51-point scorecard for all tracked companies.

### Scheduled Execution (Windows Task Scheduler)
To register an automated daily background task that runs every evening after market close:
```powershell
powershell -ExecutionPolicy Bypass -File .\setup_equity_daily_cron.ps1
```

---

## Excel Report Generation

To export an institutional Excel report identical to the 51-point evaluation scorecard:
```bash
python generate_vm_excel.py TCS
```
This generates a formatted `.xlsx` workbook with all 5 pillars, historical tables, scoring formulas, and visual flags.

---

## Technical Notes

- **API Endpoints provided by `equity_server.py`**:
  - `GET /` — Serves the terminal SPA interface (`equity_viewer.html`).
  - `GET /api/search?q=XYZ` — Smart search endpoint returning matching equities.
  - `GET /api/stock?symbol=XYZ` — Comprehensive company financial payload and 51-point scorecard.
  - `GET /api/chart?symbol=XYZ&days=10000` — Corporate-action adjusted price, volume, 50 DMA, and 200 DMA dataset.
  - `GET /api/description?symbol=XYZ` — Live company overview and structured segment breakup.
- **Zero External Frontend Dependencies**: All styling, canvas chart rendering, and interactions are written in vanilla HTML5/CSS/JavaScript. No Node.js or npm build steps required.

---

## License & Attribution
Proprietary & Confidential. Built for institutional equity analysis.
