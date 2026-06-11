#!/usr/bin/env python3
"""
Regional Market Report — Scraper + Formatter (ALL-IN-ONE)
===========================================================
Scrapes ~118 market data points, formats into SOP layout, prints to screen.
No subprocess, no encoding war, no pipe fragility.

Usage:
    python regional_market_report.py              # scrape fresh + format + save cache
    python regional_market_report.py --from-cache # use cached JSON (instant)
    python regional_market_report.py --json-only  # scrape fresh, dump JSON only
"""

import json, os, sys, re, subprocess
from datetime import datetime
from xml.etree import ElementTree as ET

try:
    import curl_cffi.requests as req
except ImportError:
    req = None
    import urllib.request

from bs4 import BeautifulSoup

# ──────────────────────── CONFIG ────────────────────────

TIMEOUT = 30
IMPRERSONATE = 'chrome120'
# Fallback browser fingerprints for 403 retry — curl_cffi supports many profiles
RETRY_IMPRERSONATE = ['chrome120', 'chrome110', 'chrome107', 'safari15_5', 'safari17_0', 'edge99']
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9,id;q=0.8,zh-CN;q=0.7',
}
ZH_HEADERS = {**HEADERS, 'Accept-Language': 'zh-CN,en;q=0.9,id;q=0.8'}
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache')
CACHE_JSON = os.path.join(CACHE_DIR, 'regional_raw.json')

# ──────────────────────── HELPERS ────────────────────────

def fetch(url, impersonate=IMPRERSONATE, headers=HEADERS, timeout=TIMEOUT):
    if req:
        profiles = [impersonate] + [p for p in RETRY_IMPRERSONATE if p != impersonate]
        last_err = None
        for i, profile in enumerate(profiles):
            try:
                r = req.get(url, impersonate=profile, headers=headers, timeout=timeout)
                r.raise_for_status()
                return r
            except Exception as e:
                last_err = e
                status = getattr(e, 'response', None)
                status_code = getattr(status, 'status_code', None) if status else None
                if status_code == 403 and i < len(profiles) - 1:
                    print(f"  [retry] 403 on {profile}, trying {profiles[i+1]}",
                          file=sys.stderr)
                    continue
                raise
        raise last_err
    else:
        r = urllib.request.urlopen(url, timeout=timeout)
        return r


def clean_num(s):
    if not s:
        return None
    s = s.strip().replace(',', '').replace('\u2033', '').replace('\u2757', '')
    return s


def code_from_name(name):
    mapping = {
        'dow jones': 'Dow',
        's&p 500': 'S&P 500',
        'nasdaq': 'Nasdaq',
        'ftse 100': 'FTSE',
        'dax': 'DAX',
        'cac 40': 'CAC',
        'nikkei 225': 'Nikkei',
        'hang seng': 'HSI',
        'euro stoxx 50': 'Euro Stoxx 50',
        'ftse mib': 'FTSE MIB',
        'swiss market index': 'SMI',
        'shanghai': 'Shanghai',
        'szse component': 'SZSE Component',
        'idx composite': 'IDX',
        'idx lq45': 'LQ45',
        'idx kompas 100': 'IDX Kompas 100',
        'ftse indonesia local': 'FTSE Indonesia',
        'idx30': 'IDX30',
        'idx 30': 'IDX30',
        'idx energy': 'IDXEnergy',
        'idx basic materials': 'IDX BscMat',
        'idx industrials': 'IDXIndst',
        'idx consumer non-cyclicals': 'IDXNONCYC',
        'idx healthcare': 'IDXHlthcare',
        'idx consumer cyclical': 'IDXCYCLC',
        'idx technology': 'IDX Tech',
        'idx transportation': 'IDX Transprt',
        'idx infrastructure': 'IDX Infra',
        'idx finance': 'IDX Finance',
        'idx banking': 'IDX Banking',
        'u.s. 2y': 'US2Yr',
        'u.s. 5y': 'US5Yr',
        'u.s. 10y': 'US10Yr',
        'u.s. 30y': 'US30Yr',
        'indo 10y': 'Indo10Yr',
        'indonesia 10y': 'Indo10Yr',
        'small cap 2000': 'Small Cap 2000',
        's&p 500 vix': 'S&P 500 VIX',
        'nifty 50': 'Nifty 50',
        's&p/asx 200': 'S&P/ASX 200',
        'psei composite': 'PSEi Composite',
        'set': 'SET',
        'taiwan weighted': 'Taiwan Weighted',
        'smi': 'SMI',
        'ftse mib': 'FTSE MIB',
    }
    key = name.lower().strip()
    if key in mapping:
        return mapping[key]
    return name


# ────────────────── TABLE-BASED PARSERS ──────────────────

def parse_table_pages(pages):
    results = {}
    for label, url, name_col, last_col, chg_col, chg_pct_col in pages:
        try:
            resp = fetch(url)
            bs = BeautifulSoup(resp.text, 'lxml')
            tables = bs.find_all('table')
            if not tables:
                continue
            table = tables[0]
            rows = table.find_all('tr')
            for row in rows[1:]:
                cells = row.find_all('td')
                if len(cells) <= max(name_col, last_col, chg_col, chg_pct_col):
                    continue
                name = cells[name_col].get_text(' ', strip=True)
                name_clean = re.sub(r'\s*derived$', '', name).strip()
                code = code_from_name(name_clean)
                last_txt = cells[last_col].get_text(strip=True)
                chg_txt = cells[chg_col].get_text(strip=True) if chg_col < len(cells) else ''
                pct_txt = cells[chg_pct_col].get_text(strip=True) if chg_pct_col < len(cells) else ''
                if last_txt and code:
                    results[code] = {
                        'close': clean_num(last_txt),
                        'change': clean_num(chg_txt),
                        'change_pct': pct_txt,
                        'source': label,
                    }
        except Exception as e:
            print(f"  WARN {label}: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return results


def parse_phei():
    """ICBI + Indo10Yr from PHEI (Penilai Harga Efek Indonesia)."""
    result = {}
    try:
        resp = fetch(
            'https://www.phei.co.id/en-us/Data/Fair-Prices-and-Yield',
            impersonate='chrome120',
            timeout=30,
        )
        bs = BeautifulSoup(resp.text, 'lxml')

        # ── ICBI from the header card ──
        icbi_el = bs.find(string='ICBI')
        if icbi_el:
            container = icbi_el.find_parent(class_='col-md-12')
            if not container:
                container = icbi_el.find_parent('div', class_=True)
                while container and 'col-md-12' not in container.get('class', []):
                    container = container.parent
                    if not container or container.name == 'html':
                        container = None
                        break
            if container:
                text = container.get_text('|', strip=True)
                parts = text.split('|')
                # Expected: ICBI|▲|426.4080|Previous|425.7156|Change|0.6925|Change (%)|0.16
                if len(parts) >= 9:
                    close = parts[2]
                    prev = parts[4]
                    chg = parts[6]
                    pct = parts[8]
                    result['ICBI'] = {
                        'close': close,
                        'change': chg,
                        'change_pct': f'{pct}%',
                        'source': 'PHEI',
                    }

        # ── Indo10Yr from IGSYC table ──
        tables = bs.find_all('table')
        for table in tables:
            rows = table.find_all('tr')
            if not rows:
                continue
            header = rows[0].find_all(['th', 'td'])
            header_texts = [c.get_text(strip=True) for c in header]
            if 'Tenor' not in ' '.join(header_texts):
                continue
            for row in rows[1:]:
                cells = row.find_all('td')
                if len(cells) < 3:
                    continue
                tenor = cells[0].get_text(strip=True)
                if tenor == '10.0':
                    today = cells[1].get_text(strip=True)
                    yesterday = cells[2].get_text(strip=True)
                    try:
                        t = float(today)
                        y = float(yesterday)
                        chg = round(t - y, 4)
                        pct = round((chg / y) * 100, 2) if y else 0.0
                        result['Indo10Yr'] = {
                            'close': f'{t:.4f}',
                            'change': f'{chg:+.4f}',
                            'change_pct': f'{pct:+.2f}%',
                            'source': 'PHEI',
                        }
                    except (ValueError, TypeError):
                        pass
                    break
            break  # only first tenor table
    except Exception as e:
        print(f'  WARN PHEI: {type(e).__name__}: {str(e)[:60]}', file=sys.stderr)
    return result


def parse_commodities_futures():
    results = {}
    wanted = {
        'Crude Oil WTI': 'Oil(WT)',
        'Brent Oil': 'Oil(Brn)',
        'Natural Gas': 'Ntrl Gas',
        'Gold': 'Gold',
        'Silver': 'Silver',
        'Copper': 'Copper',
        'Aluminium': 'Aluminium',
        'Nickel': 'Nickel',
        'Tin': 'Timah',
        'US Corn': 'Corn',
        'US Soybean Oil': 'SoybeanOil',
        'US Wheat': 'Wheat',
    }
    try:
        resp = fetch('https://www.investing.com/commodities/real-time-futures')
        bs = BeautifulSoup(resp.text, 'lxml')
        tables = bs.find_all('table')
        if not tables:
            return results
        table = tables[0]
        rows = table.find_all('tr')
        for row in rows[1:]:
            cells = row.find_all('td')
            if len(cells) < 7:
                continue
            name = cells[1].get_text(' ', strip=True)
            name_clean = re.sub(r'\s*derived$', '', name).strip()
            if name_clean in wanted:
                code = wanted[name_clean]
                last = cells[3].get_text(strip=True)
                chg = cells[6].get_text(strip=True) if len(cells) > 6 else ''
                pct = cells[7].get_text(strip=True) if len(cells) > 7 else ''
                results[code] = {
                    'close': clean_num(last),
                    'change': clean_num(chg),
                    'change_pct': pct,
                    'source': 'Investing Futures',
                }
    except Exception as e:
        print(f"  WARN Commodities: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return results


# ──────────────── STATE/JSON-BASED PARSERS ────────────────

def parse_instrument_page(url, label, code_name):
    result = {}
    try:
        resp = fetch(url)
        bs = BeautifulSoup(resp.text, 'lxml')
        for script in bs.find_all('script'):
            if script.get('id') == '__NEXT_DATA__':
                data = json.loads(script.string)
                state = data['props']['pageProps']['state']

                for store_key in ['commodityStore', 'indexStore', 'bondStore', 'currencyStore', 'etfStore', 'equityStore']:
                    store = state.get(store_key, {})
                    instrument = store.get('instrument', {})
                    if not instrument:
                        continue
                    price = instrument.get('price', {})
                    if price and price.get('last') is not None:
                        result = {
                            'close': str(price['last']),
                            'change': str(price.get('change', '')),
                            'change_pct': str(price.get('changePcr', '')),
                            'high': str(price.get('high', '')),
                            'low': str(price.get('low', '')),
                            'open': str(price.get('open', '')),
                            'prev_close': str(price.get('lastClose', '')),
                            'source': label,
                        }
                        break
                if not result:
                    quotes = state.get('quotesStore', {}).get('quotes', [])
                    if isinstance(quotes, list) and len(quotes) > 0:
                        q = quotes[0]
                        if q.get('last') is not None:
                            result = {
                                'close': str(q['last']),
                                'change': str(q.get('change', '')),
                                'change_pct': str(q.get('changePct', '')),
                                'source': label,
                            }
                break
    except Exception as e:
        print(f"  WARN {label}: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return {code_name: result} if result else {}


# ──────────────── YAHOO FINANCE ────────────────

def parse_yahoo_finance(ticker, code_name):
    result = {}
    try:
        url = f'https://finance.yahoo.com/quote/{ticker}/'
        resp = fetch(url)
        bs = BeautifulSoup(resp.text, 'lxml')
        qsp = bs.find('span', {'data-testid': 'qsp-price'})
        price = qsp.get_text(strip=True) if qsp else None

        if not price:
            price_el = bs.find('fin-streamer', {'data-field': 'regularMarketPrice', 'data-symbol': ticker})
            if not price_el:
                price_el = bs.find('fin-streamer', {'data-field': 'regularMarketPrice'})
            price = price_el.get('data-value') or price_el.get_text(strip=True) if price_el else None

        # First try: fin-streamer with matching data-symbol
        change_el = bs.find('fin-streamer', {'data-field': 'regularMarketChange', 'data-symbol': ticker})
        pct_el = bs.find('fin-streamer', {'data-field': 'regularMarketChangePercent', 'data-symbol': ticker})
        change = change_el.get('data-value') or change_el.get_text(strip=True) if change_el else ''
        pct = pct_el.get('data-value') or pct_el.get_text(strip=True) if pct_el else ''

        # Second try: if fin-streamer not found for this ticker, parse from parent text
        if not change_el and qsp:
            parent_txt = qsp.parent.get_text(' ', strip=True) if qsp.parent else ''
            m = re.search(r'([+-]?\d+[\d.]*)\s*\(([+-]?\d+[\d.]*)%\)', parent_txt)
            if m:
                change = m.group(1)
                pct = m.group(2)
            else:
                m2 = re.search(r'([+-]?\d+[\d.]*)\s*\(([+-]?\d+[\d.]*)', parent_txt)
                if m2:
                    change = m2.group(1)
                    pct = m2.group(2)

        if price:
            price = price.replace(',', '')
            valid_change = change or ''
            valid_pct = f"{pct}%" if pct else ''
            if valid_change and price:
                try:
                    chg_num = abs(float(valid_change))
                    close_num = float(price)
                    if pct:
                        pct_num = abs(float(pct.rstrip('%')))
                        if pct_num < 1.0 and chg_num > close_num / 10:
                            valid_change = ''
                            valid_pct = ''
                except ValueError:
                    pass
            result = {
                code_name: {
                    'close': price,
                    'change': valid_change,
                    'change_pct': valid_pct,
                    'source': 'Yahoo Finance',
                }
            }
    except Exception as e:
        print(f"  WARN {code_name} (Yahoo): {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return result


def parse_yahoo_sector_indices():
    result = {}
    sectors = [
        ("IDXEnergy", "IDXENERGY.JK"),
        ("IDX BscMat", "IDXBASIC.JK"),
        ("IDXIndst", "IDXINDUST.JK"),
        ("IDXNONCYC", "IDXNONCYC.JK"),
        ("IDXHlthcare", "IDXHEALTH.JK"),
        ("IDXCYCLC", "IDXCYCLIC.JK"),
        ("IDX Tech", "IDXTECHNO.JK"),
        ("IDX Transprt", "IDXTRANS.JK"),
        ("IDX Infra", "IDXINFRA.JK"),
        ("IDX Finance", "IDXFINANCE.JK"),
        ("IDX Banking", "INFOBANK15.JK"),
        ("IDX Property", "IDXPROPERT.JK"),
    ]
    for code_name, ticker in sectors:
        try:
            resp = fetch(
                f'https://finance.yahoo.com/quote/{ticker}/',
                impersonate='chrome120', timeout=20,
            )
            soup = BeautifulSoup(resp.text, 'lxml')
            qsp = soup.find('span', {'data-testid': 'qsp-price'})
            price_str = qsp.get_text(strip=True).replace(',', '') if qsp else None
            if not price_str:
                pe = soup.find('fin-streamer', {'data-field': 'regularMarketPrice'})
                if pe and not pe.get('data-symbol'):
                    price_str = (pe.get('data-value', '') or
                                 pe.get_text(strip=True)).replace(',', '')
            if not price_str:
                continue
            price = price_str
            change = ''
            change_pct = ''
            prev_el = soup.find('fin-streamer', {'data-field': 'regularMarketPreviousClose'})
            if prev_el:
                prev_raw = prev_el.get('data-value', '') or prev_el.get_text(strip=True)
                prev_str = prev_raw.replace(',', '')
                if prev_str:
                    try:
                        p = float(price)
                        prev = float(prev_str)
                        diff = round(p - prev, 2)
                        pct = round((diff / prev) * 100, 2) if prev != 0 else 0
                        change = f"+{diff}" if diff >= 0 else str(diff)
                        change_pct = f"+{pct}%" if pct >= 0 else f"{pct}%"
                    except (ValueError, TypeError):
                        pass
            result[code_name] = {
                'close': price,
                'change': change,
                'change_pct': change_pct,
                'source': 'Yahoo Finance (Sector)',
            }
        except Exception as e:
            print(f"  WARN {code_name} (Yahoo Sector): {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return result


# ──────────────── BONDS ────────────────

def parse_indonesia_bonds():
    results = {}
    try:
        resp = fetch(
            'https://www.investing.com/rates-bonds/indonesia-government-bonds?'
            'maturity_from=40&maturity_to=290'
        )
        bs = BeautifulSoup(resp.text, 'lxml')
        tables = bs.find_all('table')
        if tables:
            table = tables[0]
            rows = table.find_all('tr')
            for row in rows:
                cells = row.find_all('td')
                if len(cells) >= 5:
                    name = cells[1].get_text(' ', strip=True)
                    if '10Y' in name or '10 Yr' in name:
                        results['Indo10Yr'] = {
                            'close': cells[2].get_text(strip=True),
                            'prev': cells[3].get_text(strip=True),
                            'source': 'Investing Bonds',
                        }
                        break
    except Exception as e:
        print(f"  WARN Indo Bonds: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return results


# ──────────────── CDS ────────────────

def parse_indonesia_cds():
    result = {}
    try:
        payload = {
            "GLOBALVAR": {
                "FUNCTION": "CDS",
                "DOMESTIC": True,
                "ENDPOINT": "https://www.worldgovernmentbonds.com/wp-json/common/v1/historical",
                "DATE_RIF": "2099-12-31",
                "DEBUG": True,
                "OBJ": {"UNIT": "", "DECIMAL": 2, "UNIT_DELTA": "%", "DECIMAL_DELTA": 2},
                "COUNTRY1": {
                    "SYMBOL": "39", "PAESE": "Indonesia",
                    "PAESE_UPPERCASE": "INDONESIA", "BANDIERA": "id",
                    "URL_PAGE": "indonesia",
                },
                "COUNTRY2": None,
                "OBJ1": {"DURATA_STRING": "5 Years", "DURATA": 60},
                "OBJ2": None,
            }
        }
        if req:
            resp = req.post(
                'https://www.worldgovernmentbonds.com/wp-json/common/v1/historical',
                json=payload,
                headers={
                    'Origin': 'https://www.worldgovernmentbonds.com',
                    'Referer': 'https://www.worldgovernmentbonds.com/cds-historical-data/indonesia/5-years/',
                    'X-Requested-With': 'XMLHttpRequest',
                    'Content-Type': 'application/json',
                },
                impersonate='chrome120', timeout=20,
            )
        else:
            data_bytes = json.dumps(payload).encode('utf-8')
            r = urllib.request.Request(
                'https://www.worldgovernmentbonds.com/wp-json/common/v1/historical',
                data=data_bytes,
                headers={
                    'Origin': 'https://www.worldgovernmentbonds.com',
                    'Referer': 'https://www.worldgovernmentbonds.com/cds-historical-data/indonesia/5-years/',
                    'Content-Type': 'application/json',
                },
            )
            resp = urllib.request.urlopen(r, timeout=20)
            class FakeResp:
                pass
            fake = FakeResp()
            fake.text = resp.read().decode('utf-8')

            def json_method():
                return json.loads(fake.text)
            fake.json = json_method
            resp = fake

        data = resp.json()
        if not data.get('success'):
            return result
        r = data['result']
        close = str(r['ultimoValore'])
        change = ''
        change_pct = ''
        html = r.get('htmlLatestChange', '')
        if html:
            soup = BeautifulSoup(html, 'lxml')
            for tr in soup.find_all('tr'):
                cells = tr.find_all('td')
                if len(cells) >= 5 and '1 Week' in cells[0].get_text(strip=True):
                    min_div = cells[2].find('div')
                    prev_val = min_div.get_text(strip=True) if min_div else ''
                    if prev_val:
                        try:
                            curr = float(close)
                            prev = float(prev_val)
                            diff = round(curr - prev, 2)
                            pc = round((diff / prev) * 100, 2) if prev != 0 else 0
                            change = f"+{diff}" if diff >= 0 else str(diff)
                            change_pct = f"+{pc}%" if pc >= 0 else f"{pc}%"
                        except (ValueError, TypeError):
                            change = cells[1].get_text(strip=True)
                            change_pct = change
                    break
        result['IndoCDS 5yr'] = {
            'close': close,
            'change': change,
            'change_pct': change_pct,
            'source': 'WorldGovernmentBonds',
        }
    except Exception as e:
        print(f"  WARN IndoCDS: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return result


# ──────────────── AMMONIA ────────────────

def parse_ammonia():
    result = {}
    try:
        resp = fetch(
            'https://www.chemicalbook.com/PriceInfoall_CB9854275.htm',
            headers=ZH_HEADERS, timeout=15,
        )
        soup = BeautifulSoup(resp.text, 'lxml')
        entries = []
        for li in soup.find_all('li'):
            if li.get('class') and 'align_r' in li.get('class'):
                continue
            txt = li.get_text(' ', strip=True)
            m = re.search(r'(\d+月\d+日).*?氨.*?报价[:：]?(\d[\d,.]*)', txt)
            if m:
                entries.append({
                    'date': m.group(1),
                    'price': m.group(2).replace(',', ''),
                })
        if entries:
            latest = entries[0]
            close = latest['price']
            change = ''
            change_pct = ''
            if len(entries) >= 2:
                prev = entries[1]
                try:
                    c = float(close)
                    p = float(prev['price'])
                    diff = round(c - p, 2)
                    pc = round((diff / p) * 100, 2) if p != 0 else 0
                    change = f"+{diff}" if diff >= 0 else str(diff)
                    change_pct = f"+{pc}%" if pc >= 0 else f"{pc}%"
                except (ValueError, TypeError):
                    pass
            result['Ammonia'] = {
                'close': close,
                'change': change,
                'change_pct': change_pct,
                'date': latest['date'],
                'unit': 'Yuan/ton',
                'note': f"ChemicalBook ({entries[0]['date']})",
                'source': 'ChemicalBook',
            }
    except Exception as e:
        print(f"  WARN Ammonia: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return result


# ──────────────── JISDOR ────────────────

def parse_jisdor():
    result = {}
    try:
        resp = fetch(
            'https://www.bi.go.id/id/statistik/informasi-kurs/jisdor/default.aspx',
            timeout=25,
        )
        soup = BeautifulSoup(resp.text, 'lxml')
        rates = []
        for td in soup.find_all('td'):
            txt = td.get_text(strip=True)
            m = re.match(r'Rp(\d{2,3}\.\d{3})[,\s]', txt)
            if m:
                rates.append(m.group(1).replace('.', ','))
        if len(rates) >= 2:
            curr = rates[0].replace(',', '')
            prev = rates[1].replace(',', '')
            curr_f = float(curr)
            prev_f = float(prev)
            change = round(curr_f - prev_f, 0)
            change_pct = round(((curr_f - prev_f) / prev_f) * 100, 2)
            result['Jisdor'] = {
                'close': rates[0],
                'change': f'{change:+.0f}',
                'change_pct': f'{change_pct:+.2f}%',
                'source': 'BI',
            }
        elif len(rates) == 1:
            result['Jisdor'] = {
                'close': rates[0],
                'change': '',
                'change_pct': '',
                'source': 'BI',
            }
    except Exception as e:
        print(f"  WARN JISDOR: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return result


# ──────────────── DXY from Yahoo Finance API ────────────────

def parse_yahoo_dxy():
    """Fetch DXY via Yahoo Finance v8 chart API (more reliable than HTML scraping)."""
    result = {}
    try:
        url = 'https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?interval=1d&range=1d'
        resp = fetch(url, impersonate='chrome120', timeout=20)
        data = resp.json()
        meta = data.get('chart', {}).get('result', [{}])[0].get('meta', {})
        price = meta.get('regularMarketPrice')
        prev_close = meta.get('chartPreviousClose')
        if price and prev_close:
            change = round(price - prev_close, 3)
            pct = round(((price - prev_close) / prev_close) * 100, 2)
            result['USDIndx'] = {
                'close': str(price),
                'change': f'{change:+.3f}',
                'change_pct': f'{pct:+.2f}%',
                'source': 'Yahoo Finance API',
            }
        elif price:
            result['USDIndx'] = {
                'close': str(price),
                'change': '',
                'change_pct': '',
                'source': 'Yahoo Finance API',
            }
    except Exception as e:
        print(f"  WARN DXY (Yahoo API): {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return result


# ──────────────── BAR CHART COAL ────────────────

def parse_barchart_coal():
    result = {}
    month_codes = {
        "Jun": "M", "Jul": "N", "Aug": "Q", "Sep": "U",
    }

    for root_name, root_sym, label in [
        ("Newcastle", "LQ", "Coal(Nwl)"),
        ("Rotterdam", "LU", "Coal(Rot)"),
    ]:
        contracts = []
        for month_name, code in month_codes.items():
            sym = f"{root_sym}{code}26"
            try:
                resp = fetch(
                    f'https://www.barchart.com/futures/quotes/{sym}/overview',
                    impersonate='chrome120',
                    timeout=20,
                )
                soup = BeautifulSoup(resp.text, 'lxml')
                tables = soup.find_all('table')
                for table in tables:
                    rows = table.find_all('tr')
                    if rows and len(rows) > 1:
                        cells = rows[1].find_all('td')
                        if len(cells) >= 3 and cells[0].get_text(strip=True) == sym:
                            price_raw = cells[1].get_text(strip=True).replace('s', '').replace(',', '')
                            chg_raw = cells[2].get_text(strip=True).replace(',', '')
                            try:
                                price = float(price_raw)
                                chg = float(chg_raw)
                                prev_close = price - chg
                                pct = round((chg / prev_close) * 100, 2) if prev_close else 0
                                contracts.append({
                                    'month': month_name,
                                    'price': f"{price:.2f}",
                                    'change': f"{chg:+.2f}",
                                    'change_pct': f"{pct:+.2f}%",
                                })
                            except:
                                pass
                            break
            except Exception as e:
                pass

        if contracts:
            result[label] = {
                'contracts': contracts,
                'source': 'Barchart',
            }

    return result


def parse_bursa_cpo():
    """FCPO from Bursa Malaysia derivatives market table, row 3."""
    result = {}
    try:
        resp = fetch(
            'https://www.bursamalaysia.com/trade/market/derivatives_market',
            impersonate='chrome120',
            timeout=30,
        )
        bs = BeautifulSoup(resp.text, 'lxml')
        tables = bs.find_all('table')
        for table in tables:
            rows = table.find_all('tr')
            if len(rows) < 4:
                continue
            # Find the FCPO table: first header cell should say "Futures/Months"
            header_cells = rows[0].find_all(['th', 'td'])
            if not header_cells or 'Futures' not in header_cells[0].get_text(strip=True):
                continue
            # Row index 2 = 3rd row (0-based), the row the user wants
            row = rows[2]
            cells = row.find_all(['td', 'th'])
            if len(cells) < 4:
                continue
            last_raw = cells[1].get_text(strip=True).replace(',', '')
            chg_raw = cells[2].get_text(strip=True).replace(',', '')
            try:
                close = float(last_raw)
                change = float(chg_raw)
                prev_close = close - change
                pct = round((change / prev_close) * 100, 2) if prev_close else 0.0
                result['CPO'] = {
                    'close': f'{close:.2f}',
                    'change': f'{change:+.2f}',
                    'change_pct': f'{pct:+.2f}%',
                    'source': 'Bursa Malaysia',
                }
            except (ValueError, TypeError):
                pass
            break
    except Exception as e:
        print(f'  WARN Bursa CPO: {type(e).__name__}: {str(e)[:60]}', file=sys.stderr)
    return result


# ──────────────── SUNSIRS WOOD PULP ────────────────

def parse_sunsirs_woodpulp():
    """Wood pulp spot price from SunSirs Daily table (Building materials sector)."""
    result = {}
    try:
        resp = fetch('https://www.sunsirs.com/uk/sectors-17.html')
        bs = BeautifulSoup(resp.text, 'lxml')
        tables = bs.find_all('table')
        if not tables:
            return result
        # First table = Spot Price: Daily
        table = tables[0]
        rows = table.find_all('tr')
        for row in rows[1:]:  # skip header
            cells = row.find_all('td')
            if len(cells) < 5:
                continue
            name = cells[0].get_text(strip=True)
            if 'Wood pulp' in name:
                prev_raw = cells[2].get_text(strip=True).replace(',', '')
                close_raw = cells[3].get_text(strip=True).replace(',', '')
                pct_raw = cells[4].get_text(strip=True).replace('%', '').replace(',', '')
                try:
                    close = float(close_raw)
                    prev = float(prev_raw)
                    change = close - prev
                    pct = float(pct_raw)
                    result['Woodpulp'] = {
                        'close': f'{close:.2f}',
                        'change': f'{change:+.2f}',
                        'change_pct': f'{pct:+.2f}%',
                        'source': 'SunSirs',
                    }
                except (ValueError, TypeError):
                    pass
                break
    except Exception as e:
        print(f'  WARN SunSirs Woodpulp: {type(e).__name__}: {str(e)[:60]}', file=sys.stderr)
    return result


# ──────────────────── DATA COLLECTION ────────────────────

def collect_data():
    """Run all scrapers, return (data_dict, sources_list, timestamp)."""
    DATA = {}

    def log(msg):
        print(msg, file=sys.stderr, flush=True)

    log("Regional Screener -- collecting data...")
    t0 = datetime.now()

    log("Major Indices...")
    DATA.update(parse_table_pages([
        ('Major Indices', 'https://www.investing.com/indices/major-indices', 1, 2, 5, 6),
    ]))

    log("IDX Indices...")
    DATA.update(parse_table_pages([
        ('IDX Indices', 'https://www.investing.com/indices/indonesia-indices?include-major-indices=true&include-additional-indices=true&include-primary-sectors=true&include-other-indices=true', 1, 2, 5, 6),
    ]))

    log("Commodities...")
    DATA.update(parse_commodities_futures())

    log("Coal from Barchart...")
    DATA.update(parse_barchart_coal())

    log("Bursa CPO...")
    DATA.update(parse_bursa_cpo())

    log("Single pages...")
    single_pages = [
        ('Iron Ore', 'https://www.investing.com/commodities/iron-ore-62-cfr-futures', 'Iron Ore 62%'),
        ('Tin', 'https://www.investing.com/commodities/tin', 'Timah'),
        ('BCOMIN', 'https://www.investing.com/indices/bloomberg-industrial-metals', 'BCOMIN'),
        ('COMIN', 'https://www.investing.com/indices/commodity-index', 'Como Indx'),
        ('USD/IDR', 'https://www.investing.com/currencies/usd-idr', 'IDR'),
        ('EUR/USD', 'https://www.investing.com/currencies/eur-usd', 'Euro'),
        ('Gold Spot', 'https://www.investing.com/currencies/xau-usd', 'Gold(Spot)'),
    ]
    for label, url, code in single_pages:
        log(f"  {label}...")
        DATA.update(parse_instrument_page(url, label, code))

    log("SunSirs Woodpulp...")
    DATA.update(parse_sunsirs_woodpulp())

    log("US Bonds...")
    DATA.update(parse_table_pages([
        ('US Bonds', 'https://www.investing.com/rates-bonds/usa-government-bonds', 1, 2, 6, 7),
    ]))

    log("PHEI (ICBI + Indo10Yr)...")
    DATA.update(parse_phei())

    log("Yahoo Finance (VIX, ETFs, STI)...")
    for ticker, code in [('^VIX', 'VIX'), ('EIDO', 'EIDO'), ('EEM', 'EEM'), ('TLK', 'TLKM'), ('^STI', 'STI')]:
        log(f"  {code}...")
        DATA.update(parse_yahoo_finance(ticker, code))

    log("IDX Sector Indices...")
    DATA.update(parse_yahoo_sector_indices())

    log("DXY.. Yahoo API...")
    DATA.update(parse_yahoo_dxy())

    log("IndoCDS...")
    DATA.update(parse_indonesia_cds())

    log("Ammonia...")
    DATA.update(parse_ammonia())

    log("JISDOR...")
    DATA.update(parse_jisdor())

    elapsed = (datetime.now() - t0).total_seconds()
    sources = sorted(set(
        v.get('source', 'unknown') for v in DATA.values() if isinstance(v, dict)
    ))

    log(f"\nDone in {elapsed:.1f}s -- {len(DATA)} items collected")

    return DATA, sources, datetime.now().isoformat()


# ──────────────────── MARKET NEWS ────────────────────

def fetch_market_news(max_items=5):
    """Fetch latest US market news headlines from Google News RSS."""
    news = []
    urls = [
        'https://news.google.com/rss/search?q=US+stock+market&hl=en-US&gl=US&ceid=US:en',
        'https://news.google.com/rss/search?q=Wall+Street&hl=en-US&gl=US&ceid=US:en',
    ]
    seen_titles = set()
    for url in urls:
        try:
            if req:
                r = req.get(url, impersonate='chrome120', timeout=15)
                text = r.text
            else:
                r = urllib.request.urlopen(url, timeout=15)
                text = r.read().decode('utf-8', errors='replace')
            root = ET.fromstring(text)
            for item in root.iter('item'):
                title = item.findtext('title', '')
                link = item.findtext('link', '')
                if not title or not link:
                    continue
                title = title.split(' - ')[0].strip()
                key = title.lower()[:60]
                if key in seen_titles:
                    continue
                seen_titles.add(key)
                news.append({'title': title, 'url': link})
                if len(news) >= max_items:
                    return news
        except Exception:
            pass
    return news


# ──────────────────── FORMATTING HELPERS ────────────────────

def close_str(d):
    if isinstance(d, dict):
        return d.get('close', '')
    if isinstance(d, str):
        return d
    return ''


def fmt(d):
    if isinstance(d, str):
        return d
    if not isinstance(d, dict):
        return str(d)
    close = d.get('close', '')
    chg = d.get('change')
    pct = d.get('change_pct')
    if pct:
        pct_clean = pct
        if pct_clean.startswith('+'):
            pct_clean = pct_clean[1:]
        # Add % if missing (Investing API returns bare numbers)
        if pct_clean and not pct_clean.endswith('%'):
            pct_clean += '%'
        if chg and chg not in ('', 'None'):
            return f'{close} ({chg} / {pct_clean})'
        else:
            return f'{close} ({pct_clean})'
    if chg and chg not in ('', 'None'):
        return f'{close} ({chg})'
    return close


def get_change(d):
    if not isinstance(d, dict):
        return ''
    pct = d.get('change_pct', '')
    if pct and pct not in ('', '0', '0%'):
        try:
            pct_num = abs(float(pct.replace('%', '').replace('+', '').replace(',', '')))
            if pct_num > 50:
                return ''
        except (ValueError, ZeroDivisionError):
            pass
        # Add % if missing (Investing API returns bare numbers)
        if not pct.endswith('%'):
            pct += '%'
        if pct.startswith('+'):
            return pct[1:]
        return pct
    return ''


def get_point_change(d):
    if not isinstance(d, dict):
        return ''
    chg = d.get('change', '')
    if chg and chg not in ('', '0', 'None'):
        return chg
    return ''


def fmt_with_pct(d):
    if isinstance(d, str):
        return d
    close = close_str(d)
    pct = get_change(d)
    point = get_point_change(d)
    if point and pct and not point.startswith('-19'):
        return f'{close} ({point} / {pct})'
    if pct:
        if pct.startswith('+'):
            pct = pct[1:]
        return f'{close} ({pct})'
    if point and not point.startswith('-19'):
        return f'{close} ({point})'
    return close


# ──────────────────── REPORT FORMATTER ────────────────────

def format_report(data):
    """Build the full report text."""
    lines = []

    def kv(key, label=None):
        d = data.get(key)
        if d is None:
            return None
        return fmt_with_pct(d)

    def kv_full(key, label=None):
        d = data.get(key)
        if d is None:
            return None
        return fmt(d)

    # ── Header ──
    now = datetime.now()
    hari = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'][now.weekday()]
    bulan = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][now.month - 1]
    lines.append('📊 **Regional Markets Screener**')
    lines.append(f'🗓️ **{hari}, {now.day} {bulan} {now.year}**')
    lines.append('')
    lines.append('---')
    lines.append('')

    # ── Market News Summary ──
    lines.append('**📰 Market News Summary**')
    lines.append('')

    news = fetch_market_news(5)
    if news:
        lines.append('**Top Market News:**')
        for n in news:
            lines.append(f'• [{n["title"]}]({n["url"]})')
        lines.append('')

    lines.append('---')
    lines.append('')

    # ── US Indices ──
    lines.append('**🇺🇸 US Indices**')
    for key, label in [('Dow', 'Dow'), ('S&P 500', 'S&P 500'), ('Nasdaq', 'Nasdaq'),
                       ('Small Cap 2000', 'Small Cap 2000'), ('S&P 500 VIX', 'S&P 500 VIX')]:
        v = kv_full(key)
        if v:
            lines.append(f'• {label}: {v}')
    lines.append('')

    # ── Europe ──
    lines.append('**🇪🇺 Europe**')
    for key, label in [('DAX', 'DAX'), ('FTSE', 'FTSE'), ('CAC', 'CAC')]:
        v = kv_full(key)
        if v:
            lines.append(f'• {label}: {v}')
    lines.append('')

    # ── Asia ──
    lines.append('**🌏 Asia**')
    for key, label in [('Nikkei', 'Nikkei'), ('Shanghai', 'Shanghai'),
                       ('HSI', 'HSI'), ('KOSPI', 'KOSPI'),
                       ('STI', 'STI')]:
        v = kv_full(key)
        if v:
            lines.append(f'• {label}: {v}')
    lines.append('')

    # ── Indonesia ──
    lines.append('**🇮🇩 Indonesia**')
    idx_val = kv_full('IDX')
    if idx_val:
        lines.append(f'• IDX: {idx_val} 🔥')
    lq_val = kv_full('LQ45')
    if lq_val:
        lines.append(f'• LQ45: {lq_val}')
    kom_val = kv_full('IDX Kompas 100')
    if kom_val:
        lines.append(f'• Kompas 100: {kom_val}')
    jisdor_val = kv_full('Jisdor')
    if jisdor_val:
        lines.append(f'• Jisdor: {jisdor_val}')

    idx_sectors = [
        ('IDXEnergy', 'Energy'),
        ('IDX BscMat', 'Basic Materials'),
        ('IDXIndst', 'Industrial'),
        ('IDX Tech', 'Technology'),
        ('IDX Finance', 'Finance'),
        ('IDX Banking', 'Banking'),
        ('IDX Infra', 'Infrastructure'),
        ('IDX Property', 'Property'),
        ('IDX Transprt', 'Transportation'),
        ('IDXCYCLC', 'Consumer Cyclical'),
        ('IDXNONCYC', 'Consumer Non-Cyclical'),
        ('IDXHlthcare', 'Healthcare'),
    ]
    for k, label in idx_sectors:
        v = kv_full(k)
        if v:
            lines.append(f'• IDX {label}: {v}')
    lines.append('')

    icbi_val = kv_full('ICBI')
    if icbi_val:
        lines.append(f'• ICBI: {icbi_val}')
    lines.append('')

    # ── FX & Bonds ──
    lines.append('**💵 FX & Bonds**')
    idr_v = kv_full('IDR')
    if idr_v:
        lines.append(f'• USD/IDR: {idr_v}')
    euro_v = kv_full('Euro')
    if euro_v:
        lines.append(f'• EUR/USD: {euro_v}')
    dxy = data.get('USDIndx')
    if isinstance(dxy, dict):
        dxy_fmt = fmt(dxy)
        if dxy_fmt:
            lines.append(f'• DXY: {dxy_fmt}')

    for key, label in [('US10Yr', 'US10Yr'), ('US2Yr', 'US2Yr'), ('US30Yr', 'US30Yr')]:
        v = kv_full(key)
        if v:
            lines.append(f'• {label}: {v}')

    for key, label in [('Indo10Yr', 'Indo10Yr')]:
        v = kv_full(key)
        if v:
            lines.append(f'• {label}: {v}')

    icds = data.get('IndoCDS 5yr')
    if isinstance(icds, dict):
        icds_v = icds.get('close', '')
        icds_chg = icds.get('change', '')
        icds_pct = icds.get('change_pct', '')
        if icds_v:
            cds_str = f'{icds_v}'
            if icds_chg or icds_pct:
                cds_str += f' ({icds_chg} / {icds_pct})'
            lines.append(f'• IndoCDS 5yr: {cds_str}')

    lines.append('')

    # ── Energy ──
    lines.append('**🛢️ Energy**')
    for key, label, prefix in [('Oil(WT)', 'Oil WTI', '$'), ('Oil(Brn)', 'Oil Brent', '$'),
                                ('Ntrl Gas', 'Nat Gas', '$')]:
        d = data.get(key)
        if isinstance(d, dict):
            c = d.get('close', '')
            p = get_change(d)
            if c:
                lines.append(f'• {label}: {prefix}{c} ({p})' if p else f'• {label}: {prefix}{c}')

    lines.append('')

    # ── Coal (Barchart) ──
    lines.append('• **Coal (Barchart)** 🔄')
    coal_nwl = data.get('Coal(Nwl)')
    coal_rot = data.get('Coal(Rot)')

    if isinstance(coal_nwl, dict) and coal_nwl.get('contracts'):
        lines.append('• Newcastle:')
        for c in coal_nwl['contracts']:
            lines.append(f'  • {c["month"]}: {c["price"]} ({c["change"]} / {c["change_pct"]})')

    if isinstance(coal_rot, dict) and coal_rot.get('contracts'):
        lines.append('• Rotterdam:')
        for c in coal_rot['contracts']:
            lines.append(f'  • {c["month"]}: {c["price"]} ({c["change"]} / {c["change_pct"]})')
    lines.append('')

    # ── Metals & Mining ──
    lines.append('**🏗️ Metals & Mining**')
    for key, label in [('Gold(Spot)', 'Gold'), ('Silver', 'Silver'), ('Copper', 'Copper'),
                       ('Nickel', 'Nickel'), ('Timah', 'Timah'), ('Aluminium', 'Aluminium'),
                       ('Iron Ore 62%', 'Iron Ore 62%'), ('BCOMIN', 'BCOMIN')]:
        v = kv_full(key)
        if v:
            lines.append(f'• {label}: {v}')
    lines.append('')

    # ── Komoditas Lain ──
    lines.append('**🌿 Komoditas Lain**')
    for key, label in [('CPO', 'CPO'), ('Woodpulp', 'Woodpulp'),
                       ('Ammonia', 'Ammonia'), ('Corn', 'Corn'),
                       ('Wheat', 'Wheat'), ('SoybeanOil', 'Soybean Oil')]:
        if key == 'Ammonia':
            v = kv_full(key)
            if v:
                d = data.get(key, {})
                note = d.get('note', '') if isinstance(d, dict) else ''
                note_str = f' ({note})' if note else ''
                lines.append(f'• {label}: {v}{note_str}')
        else:
            v = kv_full(key)
            if v:
                lines.append(f'• {label}: {v}')
    lines.append('')

    # ── ETFs & Stocks ──
    lines.append('**📈 ETFs & Stocks**')
    for key, label in [('EIDO', 'EIDO'), ('TLKM', 'TLKM'), ('EEM', 'EEM')]:
        d = data.get(key)
        if isinstance(d, dict):
            c = close_str(d)
            p = get_change(d)
            if c:
                sp = get_point_change(d)
                if p:
                    lines.append(f'• {label}: {c} ({(sp + " / ") if sp else ""}{p})')
                else:
                    lines.append(f'• {label}: {c}')
    lines.append('')

    # ── Footer ──
    lines.append('---')
    lines.append('')
    lines.append('Broker Code: AT')
    lines.append('Desy Erawati/ DE')
    lines.append('Source: Bloomberg, Investing, IBPA, CNBC, Bursa Malaysia')
    lines.append('Copy;right: Phintraco Sekuritas')

    return '\n'.join(lines)


# ──────────────────── MAIN ────────────────────

def main():
    json_only = '--json-only' in sys.argv
    from_cache = '--from-cache' in sys.argv

    if from_cache and os.path.exists(CACHE_JSON):
        print('[Loading from cached screener data...]', file=sys.stderr, flush=True)
        with open(CACHE_JSON, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        data = raw.get('data', {})
        sources = raw.get('sources_used', [])
        ts = raw.get('timestamp', '')
    else:
        data, sources, ts = collect_data()

        # Save raw JSON to cache
        os.makedirs(CACHE_DIR, exist_ok=True)
        raw_out = {
            'timestamp': ts,
            'data': data,
            'sources_used': sources,
        }
        with open(CACHE_JSON, 'w', encoding='utf-8') as f:
            json.dump(raw_out, f, indent=2, ensure_ascii=False)

    if json_only:
        print(json.dumps({'timestamp': ts, 'data': data, 'sources_used': sources}, indent=2, ensure_ascii=False))
        return

    # Build and print report
    report = format_report(data)
    print(report, flush=True)

    # Save report to cache text file
    report_path = os.path.join(CACHE_DIR, 'regional_report.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f'\n\n[Report saved to {report_path}]', file=sys.stderr, flush=True)


if __name__ == '__main__':
    main()
