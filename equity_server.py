import http.server
import socketserver
import urllib.parse
import json
import sqlite3
import os
import sys
import re
import math

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "equity_engine", "equity_master.db")
HTML_FILE = os.path.join(BASE_DIR, "equity_viewer.html")
PORT = 8050

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def clean_typos(s):
    # Collapse 3 or more of same letter, or double 'dd' to 'd' if needed
    s = re.sub(r'(.)\1{2,}', r'\1', s)
    s = s.replace("dd", "d")
    return s

def normalize_company_name(value):
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"\b(ltd|limited)\b", "", value)
    return re.sub(r"[^a-z0-9]", "", value)

def resolve_company_identifier(cursor, identifier):
    match_queries = [
        ("isin", "SELECT * FROM companies WHERE UPPER(COALESCE(isin, '')) = UPPER(?)"),
        ("bse_code", "SELECT * FROM companies WHERE TRIM(COALESCE(bse_code, '')) = ?"),
        ("nse_symbol", "SELECT * FROM companies WHERE UPPER(symbol) = UPPER(?)"),
        ("name", "SELECT * FROM companies WHERE name = ? COLLATE NOCASE")
    ]

    for matched_by, sql in match_queries:
        rows = cursor.execute(sql, (identifier,)).fetchall()
        if matched_by == "nse_symbol" and identifier.isdigit():
            matched_by = "bse_code"
        matches = {row["symbol"]: row for row in rows}
        if len(matches) == 1:
            return next(iter(matches.values())), matched_by, []
        if matches:
            return None, matched_by, [
                {key: row[key] for key in ("symbol", "name", "isin", "bse_code")}
                for row in matches.values()
            ]

    companies = cursor.execute("SELECT * FROM companies").fetchall()
    normalized = normalize_company_name(identifier)
    name_matches = [
        row for row in companies
        if normalize_company_name(row["name"] or "") == normalized
    ]
    if len(name_matches) == 1:
        return name_matches[0], "name", []
    if name_matches:
        return None, "name", [
            {key: row[key] for key in ("symbol", "name", "isin", "bse_code")}
            for row in name_matches
        ]

    ignored_name_words = {"and", "the", "ltd", "limited", "company", "co"}
    words = [
        word for word in re.findall(r"[a-z0-9]+", identifier.casefold())
        if len(word) > 1 and word not in ignored_name_words
    ]
    if words:
        conditions = " AND ".join("name LIKE ?" for _ in words)
        rows = cursor.execute(
            f"SELECT * FROM companies WHERE {conditions} LIMIT 25",
            [f"%{word}%" for word in words]
        ).fetchall()
        matches = {row["symbol"]: row for row in rows}
        if len(matches) == 1:
            return next(iter(matches.values())), "name", []
        if matches:
            return None, "name", [
                {key: row[key] for key in ("symbol", "name", "isin", "bse_code")}
                for row in matches.values()
            ]

    return None, None, []

def clean_json_floats(obj):
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {key: clean_json_floats(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean_json_floats(value) for value in obj]
    return obj

def report_growth(values, years):
    if len(values) <= years:
        return None
    start_value = values[-years - 1]
    end_value = values[-1]
    if start_value is None or end_value is None or start_value <= 0 or end_value <= 0:
        return None
    return ((end_value / start_value) ** (1.0 / years) - 1.0) * 100.0

def report_high_score(actual, target, max_points):
    if actual is None or actual < 0:
        return None if actual is None else 0
    return min(max_points, math.floor((actual / target) * max_points + 1e-9))

def report_tier_score(actual, tiers):
    if actual is None:
        return None
    for threshold, points in tiers:
        if actual <= threshold:
            return points
    return 0

def report_at_least_score(actual, tiers):
    if actual is None:
        return None
    for threshold, points in tiers:
        if actual >= threshold:
            return points
    return 0

def build_portfolio_scorecard(cursor, symbol):
    annuals = cursor.execute(
        "SELECT * FROM annual_financials WHERE symbol = ? ORDER BY fiscal_year ASC, period ASC",
        (symbol,)
    ).fetchall()
    if not annuals:
        raise ValueError(f"No annual financials found for {symbol}")

    latest_quote = cursor.execute(
        "SELECT * FROM daily_market_quotes WHERE symbol = ? ORDER BY date DESC LIMIT 1",
        (symbol,)
    ).fetchone()
    share_rows = cursor.execute(
        "SELECT period, promoters_pct FROM shareholding WHERE symbol = ?",
        (symbol,)
    ).fetchall()
    month_numbers = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12
    }

    def period_order(row):
        match = re.search(r"([A-Za-z]{3})\s+(\d{4})", row["period"] or "")
        if not match:
            return (0, 0)
        return (int(match.group(2)), month_numbers.get(match.group(1).casefold(), 0))

    share_rows = sorted(share_rows, key=period_order)
    latest_promoter = share_rows[-1]["promoters_pct"] if share_rows else None
    sales = [row["sales"] for row in annuals]
    operating_profit = [row["operating_profit"] for row in annuals]
    eps = [row["eps"] for row in annuals]

    def cagr(values, years):
        if len(values) <= years:
            return None
        fiscal_year_gap = annuals[-1]["fiscal_year"] - annuals[-years - 1]["fiscal_year"]
        if fiscal_year_gap <= 0:
            return None
        return report_growth(values, fiscal_year_gap)

    def average_roce(years):
        if len(annuals) < years:
            return None
        values = []
        for row in annuals[-years:]:
            capital_employed = (row["net_worth"] or 0) + (row["borrowings"] or 0)
            ebit = (row["operating_profit"] or 0) - (row["depreciation"] or 0)
            if capital_employed <= 0:
                return None
            values.append(ebit / capital_employed * 100.0)
        return sum(values) / len(values)

    def cumulative_cfo_pat_ratio(years):
        if len(annuals) < years:
            return None
        period_rows = annuals[-years:]
        cumulative_pat = sum(row["pat"] or 0 for row in period_rows)
        if cumulative_pat <= 0:
            return None
        return sum(row["cfo"] or 0 for row in period_rows) / cumulative_pat * 100.0

    def format_value(value, suffix="", decimals=1):
        return "—" if value is None else f"{value:.{decimals}f}{suffix}"

    def criterion(name, actual, actual_text, full_marks_at, score, max_points):
        return {
            "criterion": name,
            "actual": actual_text,
            "full_marks_at": full_marks_at,
            "score": score,
            "pts": max_points
        }

    sales_1y = report_growth(sales, 1)
    sales_3y = cagr(sales, 3)
    op_1y = report_growth(operating_profit, 1)
    op_3y = cagr(operating_profit, 3)
    eps_1y = report_growth(eps, 1)
    eps_3y = cagr(eps, 3)
    roce_3y = average_roce(3)
    roce_5y = average_roce(5)
    cfo_pat_3y = cumulative_cfo_pat_ratio(3)
    cfo_pat_5y = cumulative_cfo_pat_ratio(5)
    latest_annual = annuals[-1]
    debt_equity = None
    net_worth = latest_annual["net_worth"]
    if net_worth:
        debt_equity = (latest_annual["borrowings"] or 0) / net_worth
    pe_ratio = latest_quote["pe"] if latest_quote else None
    dividend_yield = latest_quote["div_yield"] if latest_quote else None
    peg_ratio = pe_ratio / eps_3y if pe_ratio is not None and eps_3y and eps_3y > 0 else None

    groups = [
        {
            "name": "Growth",
            "criteria": [
                criterion("Sales growth · 1 yr", sales_1y, format_value(sales_1y, "%"), "≥ 15%", report_high_score(sales_1y, 15, 3), 3),
                criterion("Sales growth · 3 yr CAGR", sales_3y, format_value(sales_3y, "%"), "≥ 15%", report_high_score(sales_3y, 15, 3), 3),
                criterion("Operating profit growth · 1 yr", op_1y, format_value(op_1y, "%"), "≥ 20%", report_high_score(op_1y, 20, 3), 3),
                criterion("Operating profit growth · 3 yr CAGR", op_3y, format_value(op_3y, "%"), "≥ 20%", report_high_score(op_3y, 20, 3), 3),
                criterion("EPS growth · 1 yr", eps_1y, format_value(eps_1y, "%"), "≥ 20%", report_high_score(eps_1y, 20, 3), 3),
                criterion("EPS growth · 3 yr CAGR", eps_3y, format_value(eps_3y, "%"), "≥ 20%", report_high_score(eps_3y, 20, 3), 3)
            ]
        },
        {
            "name": "Quality",
            "criteria": [
                criterion("ROCE · 3 yr average", roce_3y, format_value(roce_3y, "%"), "≥ 20%", report_at_least_score(roce_3y, [(20, 3), (15, 2), (12, 1)]), 3),
                criterion("ROCE · 5 yr average", roce_5y, format_value(roce_5y, "%"), "≥ 20%", report_at_least_score(roce_5y, [(20, 3), (15, 2), (12, 1)]), 3),
                criterion("Cumulative CFO / PAT · 3 yr", cfo_pat_3y, format_value(cfo_pat_3y, "%"), "≥ 100%", report_high_score(cfo_pat_3y, 100, 4), 4),
                criterion("Cumulative CFO / PAT · 5 yr", cfo_pat_5y, format_value(cfo_pat_5y, "%"), "≥ 100%", report_high_score(cfo_pat_5y, 100, 4), 4),
                criterion("Debt to equity", debt_equity, format_value(debt_equity, "", 2), "≤ 0.2", report_tier_score(debt_equity, [(0.2, 3), (0.5, 2), (0.75, 1)]), 3),
                criterion("Promoter holding", latest_promoter, format_value(latest_promoter, "%"), "≥ 51%", None if latest_promoter is None else (2 if latest_promoter >= 51 else 1 if latest_promoter >= 40 else 0), 2)
            ]
        },
        {
            "name": "Valuation",
            "criteria": [
                criterion("P/E ratio", pe_ratio, format_value(pe_ratio, "", 1), "≤ 15", report_tier_score(pe_ratio, [(15, 4), (18, 3), (20, 2), (25, 1)]), 4),
                criterion("PEG ratio", peg_ratio, format_value(peg_ratio, "", 2), "≤ 0.8", report_tier_score(peg_ratio, [(0.8, 3), (1.0, 2), (1.5, 1)]), 3),
                criterion("Dividend yield", dividend_yield, format_value(dividend_yield, "%"), "≥ 3%", report_high_score(dividend_yield, 3, 3), 3)
            ]
        }
    ]

    for group in groups:
        group["score"] = sum(item["score"] or 0 for item in group["criteria"])
        group["max_score"] = sum(item["pts"] for item in group["criteria"])
        complete = all(item["score"] is not None for item in group["criteria"])
        group["percentage"] = round(group["score"] / group["max_score"] * 100) if complete and group["max_score"] else None

    total_score = sum(group["score"] for group in groups)
    complete = all(item["score"] is not None for group in groups for item in group["criteria"])
    return {
        "total_score": total_score,
        "max_total": sum(group["max_score"] for group in groups),
        "score_pct": round(total_score / 47 * 100, 1) if complete else None,
        "groups": groups
    }

DESC_CACHE = {}
CHART_CACHE = {}

def resolve_screener_id(symbol, url=None):
    import requests
    from bs4 import BeautifulSoup
    if not url:
        url = f"https://www.screener.in/company/{symbol}/"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=8)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.content, "html.parser")
            info = soup.find("div", id="company-info")
            sid = info.get("data-company-id") if info else None
            if not sid and info:
                sid = info.get("data-warehouse-id")
            return sid
    except Exception as e:
        print(f"Error resolving screener id for {symbol}: {e}")
    return None

def fetch_screener_description(symbol, url=None):
    if symbol in DESC_CACHE:
        return DESC_CACHE[symbol]
    import requests
    from bs4 import BeautifulSoup
    if not url:
        url = f"https://www.screener.in/company/{symbol}/"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=8)
        if resp.status_code != 200:
            return {"symbol": symbol, "overview": "Company overview not available.", "highlights": []}
        soup = BeautifulSoup(resp.content, "html.parser")
        
        # 1. Clean About Paragraph (Standard Company Profile)
        about_el = soup.select_one(".about")
        overview = ""
        if about_el:
            for s in about_el.find_all(["sup", "a"]):
                s.decompose()
            overview = re.sub(r'\[\d+\]', '', about_el.text).strip()
            overview = re.sub(r'\s+', ' ', overview)
            
        # 2. Key Highlights from Commentary (Breakdowns, Segments, Highlights)
        comm_el = soup.select_one(".commentary")
        highlights = []
        if comm_el:
            for a in comm_el.find_all(["sup", "a"]):
                a.decompose()
            paragraphs = comm_el.find_all("p")
            for p in paragraphs:
                strong = p.find("strong")
                title = strong.text.strip() if strong else ""
                text = p.get_text(separator="\n").strip()
                if title:
                    text = text.replace(title, "").strip()
                raw_lines = [l.strip() for l in text.split("\n") if l.strip()]
                
                # Check if lines are structured key-value pairs (e.g. 'BFSI : 31.9%')
                structured_items = []
                is_kv = True
                for l in raw_lines:
                    if ":" in l:
                        parts = l.split(":", 1)
                        structured_items.append({"label": parts[0].strip(), "value": parts[1].strip()})
                    else:
                        is_kv = False
                        break
                
                if is_kv and structured_items:
                    highlights.append({"title": title or "Revenue / Segment Breakup", "type": "key_value", "items": structured_items})
                elif raw_lines:
                    cleaned_body = " ".join(raw_lines)
                    cleaned_body = re.sub(r'\s+', ' ', cleaned_body).strip()
                    highlights.append({"title": title or "Operational Profile", "type": "narrative", "text": cleaned_body})
                    
        result = {"symbol": symbol, "overview": overview or "Company overview not available.", "highlights": highlights}
        DESC_CACHE[symbol] = result
        return result
    except Exception as e:
        print(f"Error fetching description for {symbol}: {e}")
        return {"symbol": symbol, "overview": "Company overview could not be loaded.", "highlights": []}

class EquityHandler(http.server.SimpleHTTPRequestHandler):
    def send_json(self, status, payload):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(clean_json_floats(payload), default=str, allow_nan=False).encode("utf-8"))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # 1. Root / Dashboard page
        if path == "/" or path == "/index" or path == "/equity":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if os.path.exists(HTML_FILE):
                with open(HTML_FILE, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.wfile.write(b"<h1>equity_viewer.html not found.</h1>")
            return

        # 2. Smart Multi-Word & Typo-Tolerant Search API
        if path == "/api/search":
            raw_q = query.get("q", [""])[0].strip()
            conn = get_db()
            c = conn.cursor()
            results = []

            if raw_q:
                # Normalize query
                words = [w for w in re.split(r'[\s\-_]+', raw_q) if w]
                no_space_q = re.sub(r'[^a-zA-Z0-9]', '', raw_q).upper()
                cleaned_q = clean_typos(raw_q)
                cleaned_words = [w for w in re.split(r'[\s\-_]+', cleaned_q) if w]

                # Strategy A: Exact symbol or no-space symbol prefix
                c.execute("""
                    SELECT symbol, name, bse_code, isin, sector
                    FROM companies
                    WHERE symbol = ? OR symbol LIKE ? OR bse_code = ?
                    LIMIT 10
                """, (no_space_q, f"{no_space_q}%", raw_q))
                results.extend([dict(r) for r in c.fetchall()])

                # Strategy B: All words must match in company name (e.g. 'gandhi tube' -> name has 'gandhi' AND 'tube')
                if len(results) < 10 and words:
                    conditions = " AND ".join(["name LIKE ?" for _ in words])
                    params = [f"%{w}%" for w in words]
                    c.execute(f"""
                        SELECT symbol, name, bse_code, isin, sector
                        FROM companies
                        WHERE {conditions}
                        LIMIT 10
                    """, params)
                    for r in c.fetchall():
                        d = dict(r)
                        if not any(x['symbol'] == d['symbol'] for x in results):
                            results.append(d)

                # Strategy C: Typo-cleaned words (e.g. 'ganddhi' -> 'gandhi')
                if len(results) < 5 and cleaned_words != words:
                    conditions = " AND ".join(["name LIKE ?" for _ in cleaned_words])
                    params = [f"%{w}%" for w in cleaned_words]
                    c.execute(f"""
                        SELECT symbol, name, bse_code, isin, sector
                        FROM companies
                        WHERE {conditions}
                        LIMIT 10
                    """, params)
                    for r in c.fetchall():
                        d = dict(r)
                        if not any(x['symbol'] == d['symbol'] for x in results):
                            results.append(d)

                # Strategy D: Any word match
                if len(results) == 0 and words:
                    c.execute("""
                        SELECT symbol, name, bse_code, isin, sector
                        FROM companies
                        WHERE name LIKE ? OR symbol LIKE ?
                        LIMIT 10
                    """, (f"%{words[0]}%", f"%{words[0]}%"))
                    results.extend([dict(r) for r in c.fetchall()])
            else:
                c.execute("""
                    SELECT symbol, name, bse_code, isin, sector
                    FROM companies
                    WHERE symbol IN ('FRONTSP', 'GANDHITUBE', 'TCS', 'RELIANCE', 'ABB', 'BAJFINANCE')
                    LIMIT 10
                """)
                results = [dict(r) for r in c.fetchall()]

            conn.close()

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(results[:15]).encode("utf-8"))
            return

        # 3. Scorecard lookup by ISIN, name, NSE symbol, or BSE code.
        if path == "/api/scorecard":
            identifier = query.get("identifier", query.get("q", [""]))[0].strip()
            if not identifier:
                self.send_json(400, {"error": "Provide an identifier query parameter."})
                return

            conn = get_db()
            cursor = conn.cursor()
            company_row, matched_by, candidates = resolve_company_identifier(cursor, identifier)
            if candidates:
                conn.close()
                self.send_json(409, {
                    "error": "Identifier matches multiple companies.",
                    "identifier": identifier,
                    "matched_by": matched_by,
                    "candidates": candidates
                })
                return
            if not company_row:
                conn.close()
                self.send_json(404, {"error": f"No company matched '{identifier}'."})
                return

            company = dict(company_row)
            symbol = company["symbol"]
            try:
                scorecard = build_portfolio_scorecard(cursor, symbol)
            except Exception as exc:
                conn.close()
                self.send_json(422, {
                    "error": "Company was found, but its report-card data is unavailable.",
                    "company": {"symbol": symbol, "name": company["name"]},
                    "detail": str(exc)
                })
                return
            conn.close()

            self.send_json(200, {
                "query": identifier,
                "matched_by": matched_by,
                "company": {
                    key: company.get(key)
                    for key in ("symbol", "name", "isin", "bse_code", "industry", "sector")
                },
                "scorecard": scorecard
            })
            return

        # 4. Smart Stock Resolver API: /api/stock?symbol=XYZ
        if path == "/api/stock":
            raw_sym = query.get("symbol", ["FRONTSP"])[0].strip()
            conn = get_db()
            c = conn.cursor()
            comp_row = None

            # Attempt 1: Exact symbol match (e.g. 'GANDHITUBE')
            c.execute("SELECT * FROM companies WHERE symbol = ?", (raw_sym.upper(),))
            comp_row = c.fetchone()

            # Attempt 2: BSE code match (e.g. '513108')
            if not comp_row:
                c.execute("SELECT * FROM companies WHERE bse_code = ?", (raw_sym,))
                comp_row = c.fetchone()

            # Attempt 3: Stripped of spaces (e.g. 'GANDHI TUBE' -> 'GANDHITUBE')
            no_spaces = re.sub(r'[^a-zA-Z0-9]', '', raw_sym).upper()
            if not comp_row and no_spaces:
                c.execute("SELECT * FROM companies WHERE symbol = ?", (no_spaces,))
                comp_row = c.fetchone()

            # Attempt 4: Multi-word name match (e.g. 'gandhi tube' matches 'Gandhi Special Tubes Ltd')
            if not comp_row:
                words = [w for w in re.split(r'[\s\-_]+', clean_typos(raw_sym)) if w]
                if words:
                    conditions = " AND ".join(["name LIKE ?" for _ in words])
                    params = [f"%{w}%" for w in words]
                    c.execute(f"SELECT * FROM companies WHERE {conditions} LIMIT 1", params)
                    comp_row = c.fetchone()

            # Attempt 5: Fuzzy search first token
            if not comp_row:
                first_token = re.split(r'[\s\-_]+', clean_typos(raw_sym))[0]
                if len(first_token) >= 3:
                    c.execute("SELECT * FROM companies WHERE symbol LIKE ? OR name LIKE ? LIMIT 1", 
                              (f"%{first_token.upper()}%", f"%{first_token}%"))
                    comp_row = c.fetchone()

            if not comp_row:
                conn.close()
                self.send_response(404)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": f"Stock '{raw_sym}' not found"}).encode("utf-8"))
                return

            comp = dict(comp_row)
            actual_sym = comp['symbol']

            # Market quote
            c.execute("SELECT * FROM daily_market_quotes WHERE symbol = ? ORDER BY date DESC LIMIT 1", (actual_sym,))
            quote_row = c.fetchone()
            quote = dict(quote_row) if quote_row else {}

            # Annual financials
            c.execute("SELECT * FROM annual_financials WHERE symbol = ? ORDER BY fiscal_year ASC", (actual_sym,))
            annuals = [dict(r) for r in c.fetchall()]

            # Quarterly financials
            c.execute("SELECT * FROM quarterly_financials WHERE symbol = ? ORDER BY period ASC", (actual_sym,))
            quarters = [dict(r) for r in c.fetchall()]

            # Shareholding
            c.execute("SELECT * FROM shareholding WHERE symbol = ? ORDER BY period ASC", (actual_sym,))
            shareholding = [dict(r) for r in c.fetchall()]

            # Scorecard Analysis
            c.execute("SELECT * FROM analysis_results WHERE symbol = ?", (actual_sym,))
            analysis_row = c.fetchone()
            analysis = {}
            if analysis_row:
                analysis = dict(analysis_row)
                if analysis.get('parameters_json'):
                    try:
                        analysis['parameters'] = json.loads(analysis['parameters_json'])
                    except Exception:
                        analysis['parameters'] = {}
                else:
                    analysis['parameters'] = {}

            conn.close()

            # Ensure strictly 51-point scorecard
            if not analysis or analysis.get('max_total') != 51.0:
                try:
                    from equity_engine.calculator import run_vm_analysis
                    summary = run_vm_analysis(actual_sym)
                    analysis = summary
                except Exception as e:
                    print(f"Error computing 51-point analysis for {actual_sym}: {e}")

            data = {
                "company": comp,
                "quote": quote,
                "annuals": annuals,
                "quarters": quarters,
                "shareholding": shareholding,
                "analysis": analysis
            }
            clean_data = clean_json_floats(data)

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(clean_data, default=str).encode("utf-8"))
            return

        # 5. Direct Screener Chart API Proxy: /api/chart?symbol=XYZ&days=10000
        if path == "/api/chart":
            raw_sym = query.get("symbol", ["FRONTSP"])[0].strip()
            days_param = query.get("days", ["10000"])[0].strip()
            conn = get_db()
            c = conn.cursor()

            # Attempt to resolve symbol
            c.execute("SELECT symbol, screener_id, screener_url FROM companies WHERE symbol = ?", (raw_sym.upper(),))
            row = c.fetchone()
            if not row:
                c.execute("SELECT symbol, screener_id, screener_url FROM companies WHERE bse_code = ?", (raw_sym,))
                row = c.fetchone()
            if not row:
                c.execute("SELECT symbol, screener_id, screener_url FROM companies WHERE name LIKE ? LIMIT 1", (f"%{raw_sym}%",))
                row = c.fetchone()

            if not row:
                conn.close()
                self.send_response(404)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"error": f"Symbol {raw_sym} not found"}).encode("utf-8"))
                return

            actual_sym = row['symbol']
            sid = row['screener_id']
            s_url = row['screener_url']

            if not sid:
                sid = resolve_screener_id(actual_sym, s_url)
                if sid:
                    c.execute("UPDATE companies SET screener_id = ? WHERE symbol = ?", (sid, actual_sym))
                    conn.commit()
            conn.close()

            if not sid:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"error": f"Could not find Screener ID for {actual_sym}"}).encode("utf-8"))
                return

            cache_key = f"{actual_sym}_{days_param}"
            if cache_key in CHART_CACHE:
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(CHART_CACHE[cache_key])
                return

            try:
                import requests
                chart_url = f"https://www.screener.in/api/company/{sid}/chart/?q=Price-Volume-DMA50-DMA200&days={days_param}"
                res = requests.get(chart_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
                if res.status_code == 200:
                    CHART_CACHE[cache_key] = res.content
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(res.content)
                    return
                else:
                    self.send_response(res.status_code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": f"Screener returned {res.status_code}"}).encode("utf-8"))
                    return
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
                return

        # 6. Live Company Description Proxy: /api/description?symbol=XYZ
        if path == "/api/description":
            raw_sym = query.get("symbol", ["FRONTSP"])[0].strip()
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT symbol, screener_url FROM companies WHERE symbol = ?", (raw_sym.upper(),))
            row = c.fetchone()
            if not row:
                c.execute("SELECT symbol, screener_url FROM companies WHERE bse_code = ?", (raw_sym,))
                row = c.fetchone()
            if not row:
                c.execute("SELECT symbol, screener_url FROM companies WHERE name LIKE ? LIMIT 1", (f"%{raw_sym}%",))
                row = c.fetchone()
            conn.close()
            actual_sym = row['symbol'] if row else raw_sym.upper()
            s_url = row['screener_url'] if row else None
            desc_data = fetch_screener_description(actual_sym, s_url)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(desc_data).encode("utf-8"))
            return

        # Fallback to default static file handler
        return super().do_GET()

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

def start_server():
    server = ThreadedHTTPServer(("", PORT), EquityHandler)
    print(f"============================================================")
    print(f"  EQUITY RESEARCH VIEWER SERVER (THREADED) RUNNING")
    print(f"  URL: http://localhost:{PORT}/?symbol=FRONTSP")
    print(f"============================================================")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")

if __name__ == "__main__":
    start_server()
