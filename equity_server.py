import http.server
import socketserver
import urllib.parse
import json
import sqlite3
import os
import sys
import re

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

        # 3. Smart Stock Resolver API: /api/stock?symbol=XYZ
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

            import math
            def clean_json_floats(obj):
                if isinstance(obj, float):
                    if math.isnan(obj) or math.isinf(obj):
                        return None
                    return obj
                elif isinstance(obj, dict):
                    return {k: clean_json_floats(v) for k, v in obj.items()}
                elif isinstance(obj, list):
                    return [clean_json_floats(v) for v in obj]
                return obj

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

        # 4. Direct Screener Chart API Proxy: /api/chart?symbol=XYZ&days=10000
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

        # 5. Live Company Description Proxy: /api/description?symbol=XYZ
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
