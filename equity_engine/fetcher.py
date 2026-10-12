import requests
import re
import pandas as pd
from bs4 import BeautifulSoup
from io import StringIO
from datetime import datetime
from .database import get_connection, init_db

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5'
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
adapter = requests.adapters.HTTPAdapter(pool_connections=25, pool_maxsize=25, max_retries=1)
SESSION.mount('https://', adapter)
SESSION.mount('http://', adapter)

def clean_num(val):
    if val is None or pd.isna(val):
        return 0.0
    s = str(val).replace(',', '').replace('\u20b9', '').replace('Cr.', '').replace('%', '').strip()
    if s == '' or s == '--' or s == '-':
        return 0.0
    # Handle negative formatted as (123)
    if s.startswith('(') and s.endswith(')'):
        s = '-' + s[1:-1]
    try:
        return float(s)
    except:
        return 0.0

def resolve_company(query):
    query_str = str(query).strip()
    # Check if direct ticker
    direct_url = f"https://www.screener.in/company/{query_str.upper()}/consolidated/"
    try:
        r = SESSION.get(direct_url, timeout=8)
        if r.status_code == 200:
            return query_str.upper(), direct_url, r.content
    except Exception as e:
        if any(w in str(e).lower() for w in ['reset', 'connection', 'timeout', '10054']):
            raise ConnectionError(f"Connection reset/timeout on {direct_url}: {e}")
    
    standalone_url = f"https://www.screener.in/company/{query_str.upper()}/"
    try:
        r = SESSION.get(standalone_url, timeout=8)
        if r.status_code == 200:
            return query_str.upper(), standalone_url, r.content
    except Exception as e:
        if any(w in str(e).lower() for w in ['reset', 'connection', 'timeout', '10054']):
            raise ConnectionError(f"Connection reset/timeout on {standalone_url}: {e}")
        
    # Search API
    search_url = f"https://www.screener.in/api/company/search/?q={query_str}"
    try:
        sr = SESSION.get(search_url, timeout=8)
        data = sr.json()
        if data and len(data) > 0:
            first = data[0]
            url = f"https://www.screener.in{first['url']}"
            sym = first['url'].strip('/').split('/')[1]
            r_page = SESSION.get(url, timeout=8)
            if r_page.status_code == 200:
                return sym, url, r_page.content
            return sym, url, None
    except Exception as e:
        if any(w in str(e).lower() for w in ['reset', 'connection', 'timeout', '10054']):
            raise ConnectionError(f"Connection reset/timeout on search: {e}")
        print(f"Search resolution error: {e}")
        
    return None, None, None

def parse_html_table(table_elem):
    if not table_elem:
        return pd.DataFrame()
    df = pd.read_html(StringIO(str(table_elem)))[0]
    df.rename(columns={df.columns[0]: 'Metric'}, inplace=True)
    df['Metric'] = df['Metric'].apply(lambda x: re.sub(r'[^a-zA-Z0-9\s%()\-]', '', str(x)).strip())
    return df

def fetch_and_store_company(query, db_path=None):
    init_db()
    symbol, url, content = resolve_company(query)
    if not symbol:
        raise ValueError(f"Could not find company for query '{query}'")
        
    if not content:
        resp = SESSION.get(url, timeout=12)
        if resp.status_code != 200:
            raise ConnectionError(f"Failed to fetch data from {url} (Status: {resp.status_code})")
        content = resp.content
        
    soup = BeautifulSoup(content, 'html.parser')
    
    # 1. Company Meta
    h1 = soup.find('h1')
    comp_name = h1.text.strip() if h1 else symbol
    
    # Sector / Industry from breadcrumbs
    # Screener breadcrumb structure: Home > Sector > Industry  (sometimes just Home > Industry)
    crumbs = soup.select('.breadcrumbs a')
    sector = ""
    industry = ""
    if len(crumbs) >= 3:
        # Home, Sector, Industry
        sector = crumbs[-2].text.strip()
        industry = crumbs[-1].text.strip()
    elif len(crumbs) == 2:
        # Home, Industry (no separate sector shown)
        sector = crumbs[-1].text.strip()
        industry = crumbs[-1].text.strip()
    elif len(crumbs) == 1:
        sector = crumbs[0].text.strip()
        industry = crumbs[0].text.strip()
    # Remove generic "Home" label if it crept in
    if sector.lower() in ('home', ''):
        sector = ""
    if industry.lower() in ('home', ''):
        industry = ""
    is_financial = any(w in (sector + industry).lower() for w in ['bank', 'financial', 'finance', 'nbfc', 'housing', 'insurance'])
    
    # Check BSE Code
    bse_code = ""
    for a in soup.select('a[href*="bseindia.com"]'):
        m = re.search(r'scripcode=(\d+)', a['href'])
        if m:
            bse_code = m.group(1)
            break
            
    # 2. Top Ratios
    top_ratios = {}
    for li in soup.select('li.flex.flex-space-between'):
        n = li.select_one('.name')
        v = li.select_one('.value')
        if n and v:
            name = n.text.strip()
            val_txt = v.text.strip().replace('\u20b9', '').replace('\n', ' ').strip()
            top_ratios[name] = val_txt

    cmp_val = clean_num(top_ratios.get('Current Price'))
    mkt_cap = clean_num(top_ratios.get('Market Cap'))
    pe_val = clean_num(top_ratios.get('Stock P/E'))
    bv_val = clean_num(top_ratios.get('Book Value'))
    div_y = clean_num(top_ratios.get('Dividend Yield'))
    roce_v = clean_num(top_ratios.get('ROCE'))
    roe_v = clean_num(top_ratios.get('ROE'))
    fv_val = clean_num(top_ratios.get('Face Value'))
    
    # 3. Tables
    def get_section_df(sec_id):
        sec = soup.find('section', id=sec_id)
        if sec:
            return parse_html_table(sec.find('table'))
        return pd.DataFrame()

    df_pl = get_section_df('profit-loss')
    df_bs = get_section_df('balance-sheet')
    df_cf = get_section_df('cash-flow')
    df_q = get_section_df('quarters')
    df_sh = get_section_df('shareholding')
    
    conn = get_connection(db_path) if db_path else get_connection()
    cur = conn.cursor()
    
    # Insert Company Master
    cur.execute('''
    INSERT OR REPLACE INTO companies (symbol, name, sector, industry, bse_code, screener_url, is_financial, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ''', (symbol, comp_name, sector, industry, bse_code, url, is_financial))
    
    # Insert Daily Quote
    today_str = datetime.now().strftime('%Y-%m-%d')
    cur.execute('''
    INSERT OR REPLACE INTO daily_market_quotes (symbol, date, cmp, market_cap, pe, book_value, div_yield, roce, roe, face_value, gsec_10y)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (symbol, today_str, cmp_val, mkt_cap, pe_val, bv_val, div_y, roce_v, roe_v, fv_val, 7.0))
    
    # Check if consolidated view has sufficient history (>= 8 years)
    # If not, check if standalone has significantly more history (e.g. NESTLEIND, multinational subsidiaries)
    if "/consolidated/" in url:
        num_consol_years = len([c for c in df_pl.columns if any(m in str(c) for m in ['Mar', 'Dec', 'Jun', 'Sep'])])
        if num_consol_years < 8:
            standalone_url = f"https://www.screener.in/company/{symbol}/"
            try:
                resp_sa = SESSION.get(standalone_url, timeout=12)
                if resp_sa.status_code == 200:
                    soup_sa = BeautifulSoup(resp_sa.content, 'html.parser')
                    def get_sa_df(sec_id):
                        sec = soup_sa.find('section', id=sec_id)
                        return parse_html_table(sec.find('table')) if sec else pd.DataFrame()
                    df_pl_sa = get_sa_df('profit-loss')
                    num_sa_years = len([c for c in df_pl_sa.columns if any(m in str(c) for m in ['Mar', 'Dec', 'Jun', 'Sep'])])
                    if num_sa_years > num_consol_years:
                        df_pl = df_pl_sa
                        df_bs = get_sa_df('balance-sheet')
                        df_cf = get_sa_df('cash-flow')
                        df_q = get_sa_df('quarters')
                        df_sh = get_sa_df('shareholding')
            except Exception:
                pass

    # Normalize period headers (e.g. 'Mar 2024  15m' -> 'Mar 2024') across all tables
    def norm_period(col_name):
        m = re.search(r'(Mar|Dec|Jun|Sep)\s+\d{4}', str(col_name))
        return m.group(0) if m else str(col_name).strip()

    for df in [df_pl, df_bs, df_cf, df_q, df_sh]:
        if not df.empty:
            df.columns = [norm_period(c) if any(m in str(c) for m in ['Mar', 'Dec', 'Jun', 'Sep']) else c for c in df.columns]

    # 4. Insert Annual Financials
    year_cols = [c for c in df_pl.columns if any(m in c for m in ['Mar', 'Dec', 'Jun', 'Sep']) and c in df_bs.columns] if not df_pl.empty and not df_bs.empty else []

    if len(year_cols) > 0:
        # Sort year columns chronologically to track Balance Sheet changes
        def get_yr(c):
            m = re.search(r'\d{4}', str(c))
            return int(m.group(0)) if m else 0
        year_cols = sorted(year_cols, key=get_yr)

        prev_fa = None
        prev_cwip = None

        for col in year_cols:
            def metric_val(df, metric_pattern):
                if df.empty:
                    return 0.0
                matched = df[df['Metric'].str.lower().str.contains(metric_pattern.lower(), na=False)]
                if not matched.empty and col in matched.columns:
                    return clean_num(matched[col].values[0])
                return 0.0

            sales = metric_val(df_pl, 'sales') or metric_val(df_pl, 'revenue')
            exp = metric_val(df_pl, 'expenses') or metric_val(df_pl, 'operating expenses')
            op = metric_val(df_pl, 'operating profit') or metric_val(df_pl, 'financing profit')
            opm = metric_val(df_pl, 'opm')
            oi = metric_val(df_pl, 'other income')
            intr = metric_val(df_pl, 'interest')
            depn = metric_val(df_pl, 'depreciation')
            pbt = metric_val(df_pl, 'profit before tax')
            tax_pct = metric_val(df_pl, 'tax %')
            pat = metric_val(df_pl, 'net profit')
            eps = metric_val(df_pl, 'eps')
            div_pct = metric_val(df_pl, 'dividend payout')
            div_amt = (pat * div_pct / 100.0) if pat > 0 and div_pct > 0 else 0.0

            eq_cap = metric_val(df_bs, 'equity capital')
            res = metric_val(df_bs, 'reserves')
            nw = eq_cap + res
            debt = metric_val(df_bs, 'borrowings')
            oliab = metric_val(df_bs, 'other liabilities')
            fa = metric_val(df_bs, 'fixed assets')
            cwip = metric_val(df_bs, 'cwip')
            inv = metric_val(df_bs, 'investments')
            oass = metric_val(df_bs, 'other assets')
            
            cfo = metric_val(df_cf, 'operating activity')
            cfi = metric_val(df_cf, 'investing activity')

            # Dr. Vijay Malik Balance Sheet Capex: (FA_t + CWIP_t) - (FA_{t-1} + CWIP_{t-1}) + Depn_t
            if prev_fa is not None and prev_cwip is not None and (fa > 0 or cwip > 0):
                calc_capex = (fa + cwip) - (prev_fa + prev_cwip) + depn
                capex = calc_capex if calc_capex >= 0 else (abs(cfi) if cfi != 0 else 0.0)
            else:
                capex = abs(cfi) if cfi != 0 else 0.0

            prev_fa = fa
            prev_cwip = cwip
            fcf = cfo - capex

            # Extract fiscal year integer
            fy_m = re.search(r'\d{4}', col)
            f_year = int(fy_m.group(0)) if fy_m else 0
            
            cur.execute('''
            INSERT OR REPLACE INTO annual_financials (
                symbol, period, fiscal_year, sales, expenses, operating_profit, opm_pct,
                other_income, interest, depreciation, pbt, tax_amount, tax_pct, pat, eps,
                dividend_amount, div_payout_pct, equity_capital, reserves, net_worth,
                borrowings, other_liabilities, fixed_assets, cwip, investments, other_assets,
                cfo, capex, fcf
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                symbol, col, f_year, sales, exp, op, opm,
                oi, intr, depn, pbt, (pbt * tax_pct / 100.0), tax_pct, pat, eps,
                div_amt, div_pct, eq_cap, res, nw,
                debt, oliab, fa, cwip, inv, oass,
                cfo, capex, fcf
            ))

    # 5. Insert Quarterly Financials
    if not df_q.empty:
        q_cols = [c for c in df_q.columns if any(m in c for m in ['Jun', 'Sep', 'Dec', 'Mar']) and re.search(r'\d{4}', c)]
        for col in q_cols:
            def q_val(pattern):
                m = df_q[df_q['Metric'].str.lower().str.contains(pattern.lower(), na=False)]
                if not m.empty and col in m.columns:
                    return clean_num(m[col].values[0])
                return 0.0

            s = q_val('sales') or q_val('revenue')
            e = q_val('expenses')
            op = q_val('operating profit') or q_val('financing profit')
            opm = q_val('opm')
            oi = q_val('other income')
            intr = q_val('interest')
            depn = q_val('depreciation')
            pbt = q_val('profit before tax')
            tax = q_val('tax %')
            pat = q_val('net profit')
            eps = q_val('eps')

            cur.execute('''
            INSERT OR REPLACE INTO quarterly_financials (
                symbol, period, sales, expenses, operating_profit, opm_pct,
                other_income, interest, depreciation, pbt, tax_pct, pat, eps
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (symbol, col, s, e, op, opm, oi, intr, depn, pbt, tax, pat, eps))

    # 6. Insert Shareholding
    if not df_sh.empty:
        sh_cols = [c for c in df_sh.columns if any(m in c for m in ['Jun', 'Sep', 'Dec', 'Mar']) and re.search(r'\d{4}', c)]
        for col in sh_cols:
            def sh_val(pattern):
                m = df_sh[df_sh['Metric'].str.lower().str.contains(pattern.lower(), na=False)]
                if not m.empty and col in m.columns:
                    return clean_num(m[col].values[0])
                return 0.0

            p_pct = sh_val('promoter')
            f_pct = sh_val('fii')
            d_pct = sh_val('dii')
            pub_pct = sh_val('public')
            oth_pct = sh_val('other')

            cur.execute('''
            INSERT OR REPLACE INTO shareholding (symbol, period, promoters_pct, fiis_pct, diis_pct, public_pct, others_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (symbol, col, p_pct, f_pct, d_pct, pub_pct, oth_pct))

    conn.commit()
    conn.close()
    
    return {
        'symbol': symbol,
        'name': comp_name,
        'sector': sector,
        'industry': industry,
        'bse_code': bse_code,
        'cmp': cmp_val,
        'market_cap': mkt_cap,
        'pe': pe_val,
        'annual_years': len(year_cols) if 'year_cols' in locals() else 0,
        'quarters_count': len(q_cols) if 'q_cols' in locals() else 0
    }

if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "JSWSTEEL"
    res = fetch_and_store_company(q)
    print(f"Successfully fetched and stored {res['name']} ({res['symbol']}):")
    print(f"  CMP: Rs. {res['cmp']} | Market Cap: Rs. {res['market_cap']} Cr | P/E: {res['pe']}")
    print(f"  Annual Years Stored: {res['annual_years']} | Quarters Stored: {res['quarters_count']}")
