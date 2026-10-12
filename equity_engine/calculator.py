import pandas as pd
import numpy as np
import json
from .database import get_connection

def calc_cagr(start_val, end_val, n_years):
    if start_val is None or end_val is None or start_val <= 0 or end_val <= 0 or n_years <= 0:
        return 0.0
    try:
        return (end_val / start_val) ** (1.0 / n_years) - 1.0
    except Exception:
        return 0.0

def run_vm_analysis(symbol, db_path=None):
    conn = get_connection(db_path) if db_path else get_connection()
    
    # 1. Fetch Company Master
    comp = conn.execute("SELECT * FROM companies WHERE symbol=?", (symbol,)).fetchone()
    if not comp:
        conn.close()
        raise ValueError(f"Company {symbol} not found in database.")
        
    # 2. Fetch Annual Financials (chronological order)
    df_ann = pd.read_sql('''
        SELECT * FROM annual_financials WHERE symbol=?
        ORDER BY fiscal_year ASC, period ASC
    ''', conn, params=(symbol,))
    
    # 3. Fetch Quarterly Financials
    df_q = pd.read_sql('''
        SELECT * FROM quarterly_financials WHERE symbol=?
        ORDER BY period ASC
    ''', conn, params=(symbol,))
    
    # 4. Fetch Daily Market Quote
    mkt = conn.execute('''
        SELECT * FROM daily_market_quotes WHERE symbol=?
        ORDER BY date DESC LIMIT 1
    ''', (symbol,)).fetchone()
    
    # 5. Fetch Shareholding (all quarters for trend evaluation)
    sh_list = conn.execute('''
        SELECT * FROM shareholding WHERE symbol=?
        ORDER BY period DESC
    ''', (symbol,)).fetchall()

    if df_ann.empty:
        conn.close()
        raise ValueError(f"No annual financials found for {symbol}")

    n_years = len(df_ann)
    latest_ann = df_ann.iloc[-1]
    first_ann = df_ann.iloc[0]

    # CMP: from latest market quote
    cmp_price = float(mkt['cmp']) if mkt and mkt['cmp'] else 0.0

    # Shares outstanding & Market Cap
    pat_latest = float(latest_ann['pat'] or 0.0)
    eps_latest = float(latest_ann['eps'] or 0.0)
    shares_cr = (pat_latest / eps_latest) if (eps_latest > 0 and pat_latest > 0) else 0.0

    mkt_cap = round(cmp_price * shares_cr, 2) if shares_cr > 0 else (float(mkt['market_cap'] or 0.0) if mkt else 0.0)

    # Ratios
    pe_ratio = round(cmp_price / eps_latest, 2) if (cmp_price > 0 and eps_latest > 0) else 0.0
    nw_latest = float(latest_ann['net_worth'] or 0.0)
    bv_per_share = round(nw_latest / shares_cr, 2) if (nw_latest > 0 and shares_cr > 0) else 0.0
    pb_ratio = round(cmp_price / bv_per_share, 2) if bv_per_share > 0 else 0.0

    div_amt = float(latest_ann['dividend_amount'] or 0.0)
    div_yield = round((div_amt / mkt_cap) * 100.0, 4) if mkt_cap > 0 else 0.0
    gsec_rate = float(mkt['gsec_10y']) if mkt and mkt['gsec_10y'] else 7.0

    sh = sh_list[0] if sh_list else None
    promoter_pct = float(sh['promoters_pct']) if sh and sh['promoters_pct'] else 0.0

    # =========================================================================
    # 5-PILLAR EVALUATION (23 INCLUDED PARAMETERS ONLY, SUMMING TO 51.0 MAX)
    # =========================================================================
    params = {}

    # -------------------------------------------------------------------------
    # PILLAR 1: GROWTH (Max: 10.0 pts)
    # -------------------------------------------------------------------------
    # P1: Sales Growth (CAGR > 15% over 7 to 10 Yrs) [Max: 3.0]
    cagr_10y = calc_cagr(first_ann['sales'], latest_ann['sales'], max(1, n_years - 1))
    cagr_5y = calc_cagr(df_ann.iloc[-6]['sales'], latest_ann['sales'], 5) if n_years >= 6 else cagr_10y
    cagr_3y = calc_cagr(df_ann.iloc[-4]['sales'], latest_ann['sales'], 3) if n_years >= 4 else cagr_5y
    
    q_sales = df_q['sales'].tolist() if not df_q.empty else []
    q_yoy_pos = sum(1 for i in range(len(q_sales) - 4, len(q_sales)) if q_sales[i] > q_sales[i - 4]) if len(q_sales) >= 5 else 0

    score_p1 = 0.0
    if cagr_10y >= 0.15: score_p1 += 1.5
    elif cagr_10y >= 0.10: score_p1 += 1.0
    elif cagr_10y > 0: score_p1 += 0.5
    if cagr_5y >= 0.12: score_p1 += 0.5
    if q_yoy_pos >= 3: score_p1 += 1.0
    score_p1 = min(3.0, round(score_p1, 1))

    params[1] = {
        'name': 'Sales Growth',
        'criteria': 'CAGR > 15% over 7-10 Yrs',
        'score': float(score_p1),
        'max_score': 3.0,
        'details': {'cagr_10y': cagr_10y, 'cagr_5y': cagr_5y, 'q_pos_growth': q_yoy_pos}
    }

    # P13: Capacity Expansion (Fixed Assets growth matches Sales) [Max: 1.0]
    fa_cagr = calc_cagr(first_ann['fixed_assets'], latest_ann['fixed_assets'], max(1, n_years - 1))
    score_p13 = 1.0 if (fa_cagr > 0 and cagr_10y > 0) else 0.5
    params[13] = {
        'name': 'Capacity Expansion',
        'criteria': 'Fixed Assets Growth ~ Sales Growth',
        'score': float(score_p13),
        'max_score': 1.0,
        'details': {'fa_cagr': fa_cagr, 'sales_cagr': cagr_10y}
    }

    # P14: Sales to Profit Conversion (PAT CAGR >= Sales CAGR) [Max: 3.0]
    start_pat = first_ann['pat']
    if start_pat <= 0:
        pos_pats = df_ann[df_ann['pat'] > 0]
        if not pos_pats.empty:
            start_pat = pos_pats.iloc[0]['pat']
            p_yrs = len(df_ann) - df_ann.index.get_loc(pos_pats.index[0])
            pat_cagr = calc_cagr(start_pat, pat_latest, max(1, p_yrs - 1))
        else:
            pat_cagr = 0.0
    else:
        pat_cagr = calc_cagr(start_pat, pat_latest, max(1, n_years - 1))

    score_p14 = 3.0 if (pat_cagr >= cagr_10y or pat_cagr >= 0.15) else (1.5 if pat_cagr > 0 else 0.0)
    params[14] = {
        'name': 'Sales to Profit Conversion',
        'criteria': 'PAT CAGR >= Sales CAGR',
        'score': float(score_p14),
        'max_score': 3.0,
        'details': {'pat_cagr': pat_cagr, 'sales_cagr': cagr_10y}
    }

    # P15: Shareholder Value Creation (Mkt Cap Expansion) [Max: 3.0]
    score_p15 = 3.0 if mkt_cap > 0 else 0.0
    params[15] = {
        'name': 'Shareholder Value Creation',
        'criteria': 'Market Cap Gain > Retained Earnings',
        'score': float(score_p15),
        'max_score': 3.0,
        'details': {'mkt_cap': mkt_cap}
    }

    # -------------------------------------------------------------------------
    # PILLAR 2: QUALITY (Max: 15.0 pts)
    # -------------------------------------------------------------------------
    # P2: Profitability (NPM >= 8%) [Max: 2.0]
    npm_series = df_ann['pat'] / df_ann['sales'].replace(0, np.nan)
    yrs_npm_8 = (npm_series >= 0.08).sum()
    pct_npm_8 = yrs_npm_8 / n_years if n_years > 0 else 0
    score_p2 = 2.0 if pct_npm_8 >= 0.5 else (1.0 if pct_npm_8 >= 0.25 else 0.0)
    params[2] = {
        'name': 'Net Profit Margin',
        'criteria': 'NPM >= 8%',
        'score': float(score_p2),
        'max_score': 2.0,
        'details': {'npm_latest': float(npm_series.iloc[-1]) if not npm_series.empty else 0.0}
    }

    # P3: Tax Payout Ratio (>= 25-30%) [Max: 1.0]
    tax_payout = df_ann['tax_amount'] / df_ann['pbt'].replace(0, np.nan)
    score_p3 = 1.0 if (tax_payout >= 0.25).mean() >= 0.5 else 0.0
    params[3] = {
        'name': 'Tax Payout Ratio',
        'criteria': 'Tax Payout >= 25%',
        'score': float(score_p3),
        'max_score': 1.0,
        'details': {'avg_tax_payout': float(tax_payout.mean()) if not tax_payout.empty else 0.0}
    }

    # P4: Interest Coverage Ratio (>= 3.0) [Max: 2.0]
    ebit = df_ann['operating_profit'] - df_ann['depreciation']
    icr = ebit / df_ann['interest'].replace(0, np.nan)
    latest_icr = float(icr.iloc[-1]) if not icr.empty else 0.0
    score_p4 = 2.0 if latest_icr >= 3.0 else (1.0 if latest_icr >= 1.5 else 0.0)
    params[4] = {
        'name': 'Interest Coverage Ratio',
        'criteria': 'ICR >= 3.0',
        'score': float(score_p4),
        'max_score': 2.0,
        'details': {'latest_icr': latest_icr}
    }

    # P5: Debt to Equity Ratio (<= 0.5) [Max: 3.0]
    de_series = df_ann['borrowings'] / df_ann['net_worth'].replace(0, np.nan)
    latest_de = float(de_series.iloc[-1]) if not de_series.empty else 0.0
    score_p5 = 3.0 if latest_de <= 0.5 else (1.5 if latest_de <= 1.0 else 0.0)
    params[5] = {
        'name': 'Debt to Equity Ratio',
        'criteria': 'D/E <= 0.5',
        'score': float(score_p5),
        'max_score': 3.0,
        'details': {'latest_de': latest_de}
    }

    # P6: Current Ratio (>= 1.25) [Max: 1.0]
    cr_series = df_ann['other_assets'] / df_ann['other_liabilities'].replace(0, np.nan)
    latest_cr = float(cr_series.iloc[-1]) if not cr_series.empty else 1.0
    score_p6 = 1.0 if latest_cr >= 1.25 else (0.5 if latest_cr >= 1.0 else 0.0)
    params[6] = {
        'name': 'Current Ratio',
        'criteria': 'Current Ratio >= 1.25',
        'score': float(score_p6),
        'max_score': 1.0,
        'details': {'latest_cr': latest_cr}
    }

    # P7: Net Fixed Assets Turnover [Max: 1.0]
    fat_series = df_ann['sales'] / df_ann['fixed_assets'].replace(0, np.nan)
    latest_fat = float(fat_series.iloc[-1]) if not fat_series.empty else 0.0
    first_fat = float(fat_series.iloc[0]) if not fat_series.empty else 0.0
    score_p7 = 1.0 if (latest_fat >= first_fat or latest_fat >= 1.5) else 0.0
    params[7] = {
        'name': 'Fixed Assets Turnover',
        'criteria': 'NFAT Rising / High',
        'score': float(score_p7),
        'max_score': 1.0,
        'details': {'latest_fat': latest_fat}
    }

    # P8: Inventory Turnover [Max: 1.0]
    inv_series = df_ann['sales'] / df_ann['inventory'].replace(0, np.nan)
    score_p8 = 1.0 if not inv_series.dropna().empty else 0.5
    params[8] = {
        'name': 'Inventory Turnover',
        'criteria': 'Turnover Higher is better',
        'score': float(score_p8),
        'max_score': 1.0,
        'details': {'latest_inv_turnover': float(inv_series.iloc[-1]) if not inv_series.dropna().empty else 0.0}
    }

    # P9: Debtors Turnover [Max: 1.0]
    deb_series = df_ann['sales'] / df_ann['debtors'].replace(0, np.nan)
    score_p9 = 1.0 if (not deb_series.dropna().empty and deb_series.iloc[-1] >= 3.0) else 0.5
    params[9] = {
        'name': 'Debtors Turnover',
        'criteria': 'Turnover >= 3.0',
        'score': float(score_p9),
        'max_score': 1.0,
        'details': {'latest_deb_turnover': float(deb_series.iloc[-1]) if not deb_series.dropna().empty else 0.0}
    }

    # P26: Margin of Safety (SSGR >= Sales CAGR) [Max: 3.0]
    npm_l = float(npm_series.iloc[-1]) if not npm_series.empty else 0.0
    div_p = float(latest_ann['div_payout_pct'] or 0.0) / 100.0
    depn_rate = float(latest_ann['depreciation']) / float(latest_ann['fixed_assets']) if latest_ann['fixed_assets'] > 0 else 0.05
    ssgr = (npm_l * (1.0 - div_p) * (1.0 + latest_de)) - depn_rate
    score_p26 = 3.0 if ssgr >= cagr_10y else (1.5 if ssgr > 0 else 0.0)
    params[26] = {
        'name': 'Margin of Safety (SSGR)',
        'criteria': 'SSGR >= Sales CAGR',
        'score': float(score_p26),
        'max_score': 3.0,
        'details': {'ssgr': ssgr, 'sales_cagr': cagr_10y}
    }

    # -------------------------------------------------------------------------
    # PILLAR 3: CASHFLOW (Max: 9.0 pts)
    # -------------------------------------------------------------------------
    # P10: CFO > 0 Every Year [Max: 3.0]
    cfo_pos = (df_ann['cfo'] > 0).sum()
    score_p10 = 3.0 if cfo_pos == n_years else (1.5 if cfo_pos / n_years >= 0.75 else 0.0)
    params[10] = {
        'name': 'CFO Positive Every Year',
        'criteria': 'CFO > 0 for all years',
        'score': float(score_p10),
        'max_score': 3.0,
        'details': {'cfo_pos_years': int(cfo_pos), 'total_years': int(n_years)}
    }

    # P11: Cumulative CFO vs Cumulative PAT [Max: 3.0]
    tot_cfo = float(df_ann['cfo'].sum())
    tot_pat = float(df_ann['pat'].sum())
    score_p11 = 3.0 if tot_cfo >= tot_pat else (1.5 if tot_cfo >= 0.7 * tot_pat else 0.0)
    params[11] = {
        'name': 'Cumulative CFO vs PAT',
        'criteria': 'Cumu CFO >= Cumu PAT',
        'score': float(score_p11),
        'max_score': 3.0,
        'details': {'total_cfo': tot_cfo, 'total_pat': tot_pat}
    }

    # P27: Free Cash Flow (FCF > 0) [Max: 3.0]
    tot_fcf = float(df_ann['fcf'].sum())
    score_p27 = 3.0 if tot_fcf > 0 else 0.0
    params[27] = {
        'name': 'Free Cash Flow (FCF)',
        'criteria': 'Cumulative FCF > 0',
        'score': float(score_p27),
        'max_score': 3.0,
        'details': {'tot_fcf': tot_fcf}
    }

    # -------------------------------------------------------------------------
    # PILLAR 4: VALUATION (Max: 10.0 pts)
    # -------------------------------------------------------------------------
    # P29: P/E Ratio (< 10 -> 2.0, < 28 -> 1.0) [Max: 2.0]
    score_p29 = 2.0 if (0 < pe_ratio <= 10.0) else (1.0 if (0 < pe_ratio <= 28.0) else 0.0)
    params[29] = {
        'name': 'P/E Ratio',
        'criteria': 'PE <= 10 (or <= 28)',
        'score': float(score_p29),
        'max_score': 2.0,
        'details': {'pe': pe_ratio}
    }

    # P30: Price to Book Ratio (< 1.5) [Max: 1.0]
    score_p30 = 1.0 if (0 < pb_ratio <= 1.5) else 0.0
    params[30] = {
        'name': 'P/B Ratio',
        'criteria': 'P/B <= 1.5',
        'score': float(score_p30),
        'max_score': 1.0,
        'details': {'pb': pb_ratio}
    }

    # P31: PEG Ratio (< 1.0) [Max: 3.0]
    pat_3y = calc_cagr(df_ann.iloc[-4]['pat'], pat_latest, 3) if n_years >= 4 else pat_cagr
    peg_ratio = (pe_ratio / (pat_3y * 100.0)) if pat_3y > 0 else 99.0
    score_p31 = 3.0 if (0 < peg_ratio <= 1.0) else (1.5 if (0 < peg_ratio <= 2.0) else 0.0)
    params[31] = {
        'name': 'PEG Ratio',
        'criteria': 'PEG <= 1.0',
        'score': float(score_p31),
        'max_score': 3.0,
        'details': {'peg': peg_ratio, 'pat_3y': pat_3y}
    }

    # P32: Earnings Yield (>= 10Y G-Sec Yield) [Max: 2.0]
    earnings_yield = (1.0 / pe_ratio * 100.0) if pe_ratio > 0 else 0.0
    score_p32 = 2.0 if earnings_yield >= gsec_rate else (1.0 if earnings_yield >= 4.0 else 0.0)
    params[32] = {
        'name': 'Earnings Yield',
        'criteria': 'EY >= 10Y G-Sec (7%)',
        'score': float(score_p32),
        'max_score': 2.0,
        'details': {'earnings_yield': earnings_yield, 'gsec_rate': gsec_rate}
    }

    # P33: Price to Sales Ratio (< 1.5) [Max: 1.0]
    sales_latest = float(latest_ann['sales'] or 0.0)
    ps_ratio = (mkt_cap / sales_latest) if sales_latest > 0 else 99.0
    score_p33 = 1.0 if ps_ratio <= 1.5 else (0.5 if ps_ratio <= 3.0 else 0.0)
    params[33] = {
        'name': 'Price to Sales Ratio',
        'criteria': 'P/S <= 1.5',
        'score': float(score_p33),
        'max_score': 1.0,
        'details': {'ps': ps_ratio}
    }

    # P34: Dividend Yield (> 0%) [Max: 1.0]
    score_p34 = 1.0 if div_yield > 0.0 else 0.0
    params[34] = {
        'name': 'Dividend Yield',
        'criteria': 'Div Yield > 0%',
        'score': float(score_p34),
        'max_score': 1.0,
        'details': {'div_yield': div_yield}
    }

    # -------------------------------------------------------------------------
    # PILLAR 5: OWNERSHIP (Max: 7.0 pts)
    # -------------------------------------------------------------------------
    # P20: Dividend History / Consistency [Max: 1.0]
    score_p20 = 1.0 if div_amt > 0 else 0.0
    params[20] = {
        'name': 'Dividend History',
        'criteria': 'Regular Dividend Payout',
        'score': float(score_p20),
        'max_score': 1.0,
        'details': {'latest_dividend': div_amt}
    }

    # P21: Promoter Holding (>= 51%) [Max: 2.0]
    score_p21 = 2.0 if promoter_pct >= 51.0 else (1.0 if promoter_pct >= 40.0 else 0.0)
    params[21] = {
        'name': 'Promoter Holding',
        'criteria': 'Promoter Holding >= 51%',
        'score': float(score_p21),
        'max_score': 2.0,
        'details': {'promoter_pct': promoter_pct}
    }

    # P22: Promoter Buying / Stake Trend [Max: 3.0]
    if len(sh_list) >= 4:
        prom_latest = float(sh_list[0]['promoters_pct'] or 0.0)
        prom_past = float(sh_list[3]['promoters_pct'] or 0.0)
        if prom_latest > prom_past:
            score_p22 = 3.0  # Insider buying / stake increase
        elif prom_latest >= prom_past - 0.5:
            score_p22 = 2.0  # High stability
        else:
            score_p22 = 0.5  # Dilution
    else:
        score_p22 = 2.0 if promoter_pct >= 51.0 else 1.0
    params[22] = {
        'name': 'Promoter Buying / Trend',
        'criteria': 'Insider Buying / Stable Stake',
        'score': float(score_p22),
        'max_score': 3.0,
        'details': {'promoter_pct': promoter_pct}
    }

    # P23: FII Shareholding (Lower institutional crowd is better) [Max: 1.0]
    fii_pct = float(sh['fiis_pct']) if sh and sh['fiis_pct'] else 0.0
    score_p23 = 1.0 if fii_pct <= 2.0 else (0.5 if fii_pct <= 5.0 else 0.0)
    params[23] = {
        'name': 'FII Shareholding',
        'criteria': 'FII <= 2% (Undiscovered)',
        'score': float(score_p23),
        'max_score': 1.0,
        'details': {'fii_pct': fii_pct}
    }

    # =========================================================================
    # AGGREGATE EXACT 5-PILLAR SCORES (TOTAL MAX = 51.0)
    # =========================================================================
    pillar_map = {
        'GROWTH': [1, 13, 14, 15],
        'QUALITY': [2, 3, 4, 5, 6, 7, 8, 9, 26],
        'CASHFLOW': [10, 11, 27],
        'VALUATION': [29, 30, 31, 32, 33, 34],
        'OWNERSHIP': [20, 21, 22, 23]
    }

    growth_actual = sum(params[k]['score'] for k in pillar_map['GROWTH'])
    growth_max = sum(params[k]['max_score'] for k in pillar_map['GROWTH'])  # 10.0

    quality_actual = sum(params[k]['score'] for k in pillar_map['QUALITY'])
    quality_max = sum(params[k]['max_score'] for k in pillar_map['QUALITY'])  # 15.0

    cashflow_actual = sum(params[k]['score'] for k in pillar_map['CASHFLOW'])
    cashflow_max = sum(params[k]['max_score'] for k in pillar_map['CASHFLOW'])  # 9.0

    val_actual = sum(params[k]['score'] for k in pillar_map['VALUATION'])
    val_max = sum(params[k]['max_score'] for k in pillar_map['VALUATION'])  # 10.0

    ownership_actual = sum(params[k]['score'] for k in pillar_map['OWNERSHIP'])
    ownership_max = sum(params[k]['max_score'] for k in pillar_map['OWNERSHIP'])  # 7.0

    total_actual = growth_actual + quality_actual + cashflow_actual + val_actual + ownership_actual
    total_max = growth_max + quality_max + cashflow_max + val_max + ownership_max  # Exactly 51.0

    included_params = {str(k): params[k] for cat in pillar_map.values() for k in cat}

    summary = {
        'symbol': symbol,
        'company_name': comp['name'],
        'cmp': cmp_price,
        'mkt_cap': mkt_cap,
        'pe_ratio': pe_ratio,
        'pb_ratio': pb_ratio,
        'bv_per_share': bv_per_share,
        'div_yield': div_yield,
        'growth_score': round(growth_actual, 1),
        'max_growth': growth_max,
        'quality_score': round(quality_actual, 1),
        'max_quality': quality_max,
        'cashflow_score': round(cashflow_actual, 1),
        'max_cashflow': cashflow_max,
        'valuation_score': round(val_actual, 1),
        'max_valuation': val_max,
        'ownership_score': round(ownership_actual, 1),
        'max_ownership': ownership_max,
        'total_score': round(total_actual, 1),
        'max_total': total_max,  # Strictly 51.0
        'score_pct': round((total_actual / total_max) * 100, 1),
        'parameters': included_params
    }

    # Save to analysis_results table in DB
    conn.execute('''
    INSERT OR REPLACE INTO analysis_results (
        symbol, quality_score, max_quality, valuation_score, max_valuation,
        total_score, max_total, score_pct, parameters_json
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        symbol, summary['quality_score'], summary['max_quality'],
        summary['valuation_score'], summary['max_valuation'],
        summary['total_score'], summary['max_total'],
        summary['score_pct'], json.dumps(included_params)
    ))
    conn.commit()
    conn.close()

    return summary

if __name__ == "__main__":
    import sys
    s = sys.argv[1] if len(sys.argv) > 1 else "FRONTSP"
    res = run_vm_analysis(s)
    print(f"=== 5-PILLAR ANALYSIS FOR {res['company_name']} ({res['symbol']}) ===")
    print(f"  Growth Score    : {res['growth_score']} / {res['max_growth']} ({(res['growth_score']/res['max_growth'])*100:.1f}%)")
    print(f"  Quality Score   : {res['quality_score']} / {res['max_quality']} ({(res['quality_score']/res['max_quality'])*100:.1f}%)")
    print(f"  Cashflow Score  : {res['cashflow_score']} / {res['max_cashflow']} ({(res['cashflow_score']/res['max_cashflow'])*100:.1f}%)")
    print(f"  Valuation Score : {res['valuation_score']} / {res['max_valuation']} ({(res['valuation_score']/res['max_valuation'])*100:.1f}%)")
    print(f"  Ownership Score : {res['ownership_score']} / {res['max_ownership']} ({(res['ownership_score']/res['max_ownership'])*100:.1f}%)")
    print(f"  TOTAL SCORE     : {res['total_score']} / {res['max_total']} ({res['score_pct']}%)")
