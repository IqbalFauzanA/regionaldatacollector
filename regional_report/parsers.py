"""Data source parsers and collection orchestration."""

import json
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, cast
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from .commons import (
    MAX_FETCH_WORKERS,
    RETRY_IMPERSONATE,
    ZH_HEADERS,
    clean_num,
    fetch,
    has_css_class,
    is_valid_data,
    req,
)

logger = logging.getLogger(__name__)


def parse_barchart_price_change(price_raw, chg_raw):
    """Robust parsing for Barchart price and change cells.

    Handles values like '113.65', '113.65 unch', 'unch', '(unch)', '113.65 +0.50',
    and returns (price_str, change_str, change_pct_str) where any may be None.
    """

    def _find_num(x):
        if not x:
            return None
        m = re.search(r"[-+]?\d{1,3}(?:[\d,]*\d)?(?:\.\d+)?", x)
        if not m:
            return None
        try:
            return float(m.group(0).replace(",", ""))
        except Exception:
            return None

    price = _find_num(price_raw)

    chg_num = None
    if chg_raw:
        # explicit numeric change
        explicit = _find_num(chg_raw)
        if explicit is not None:
            chg_num = explicit
        # explicit 'unch' token means change is 0 but not an explicit numeric value
        elif re.search(r"\bunch\b", chg_raw, flags=re.I) or chg_raw.strip().lower() in (
            "unch",
            "(unch)",
            "unch unch",
        ):
            chg_num = 0.0

    # If price exists, compute percentage if possible
    if price is not None:
        if chg_num is None:
            # no reliable change info — treat as implicit 0 (not explicit)
            chg_num = 0.0

        prev_close = price - chg_num
        try:
            pct = round((chg_num / prev_close) * 100, 2) if prev_close else 0.0
        except Exception:
            pct = 0.0

        price_str = f"{price:.2f}"
        change_str = f"{chg_num:+.2f}" if chg_num is not None else None
        change_pct = f"{pct:+.2f}%"
        return price_str, change_str, change_pct

    # price missing but change present
    if chg_num is not None:
        price_str = None
        change_str = f"{chg_num:+.2f}"
        change_pct = ""
        return price_str, change_str, change_pct

    return None, None, None


def code_from_name(name):
    mapping = {
        "dow jones": "Dow",
        "s&p 500": "S&P 500",
        "nasdaq": "Nasdaq",
        "ftse 100": "FTSE",
        "dax": "DAX",
        "cac 40": "CAC",
        "nikkei 225": "Nikkei",
        "hang seng": "HSI",
        "euro stoxx 50": "Euro Stoxx 50",
        "ftse mib": "FTSE MIB",
        "swiss market index": "SMI",
        "shanghai": "Shanghai",
        "szse component": "SZSE Component",
        "idx composite": "IDX",
        "idx lq45": "LQ45",
        "idx kompas 100": "IDX Kompas 100",
        "ftse indonesia local": "FTSE Indonesia",
        "idx30": "IDX30",
        "idx 30": "IDX30",
        "idx energy": "IDXEnergy",
        "idx basic materials": "IDX BscMat",
        "idx industrials": "IDXIndst",
        "idx consumer non-cyclicals": "IDXNONCYC",
        "idx healthcare": "IDXHlthcare",
        "idx consumer cyclical": "IDXCYCLC",
        "idx technology": "IDX Tech",
        "idx transportation": "IDX Transprt",
        "idx infrastructure": "IDX Infra",
        "idx finance": "IDX Finance",
        "idx banking": "IDX Banking",
        "u.s. 2y": "US2Yr",
        "u.s. 5y": "US5Yr",
        "u.s. 10y": "US10Yr",
        "u.s. 30y": "US30Yr",
        "indo 10y": "Indo10Yr",
        "indonesia 10y": "Indo10Yr",
        "s&p 500 vix": "S&P 500 VIX",
        "nifty 50": "Nifty 50",
        "s&p/asx 200": "S&P/ASX 200",
        "psei composite": "PSEi Composite",
        "set": "SET",
        "taiwan weighted": "Taiwan Weighted",
        "smi": "SMI",
        "ftse mib": "FTSE MIB",
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
            bs = BeautifulSoup(resp.text, "lxml")
            tables = bs.find_all("table")
            if not tables:
                continue
            # Some pages contain multiple tables; search all of them to avoid
            # missing rows (e.g. IDX30 appearing in a later table).
            for table in tables:
                rows = table.find_all("tr")
                if not rows:
                    continue
                for row in rows[1:]:
                    cells = row.find_all("td")
                    if len(cells) <= max(name_col, last_col, chg_col, chg_pct_col):
                        continue
                    name = cells[name_col].get_text(" ", strip=True)
                    name_clean = re.sub(r"\s*derived$", "", name).strip()
                    code = code_from_name(name_clean)
                    last_txt = cells[last_col].get_text(strip=True)
                    chg_txt = (
                        cells[chg_col].get_text(strip=True)
                        if chg_col < len(cells)
                        else ""
                    )
                    pct_txt = (
                        cells[chg_pct_col].get_text(strip=True)
                        if chg_pct_col < len(cells)
                        else ""
                    )
                    if last_txt and code:
                        results[code] = {
                            "close": clean_num(last_txt),
                            "change": clean_num(chg_txt),
                            "change_pct": pct_txt,
                            "source": label,
                        }
        except Exception as e:
            print(f"  WARN {label}: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return results


def parse_phei():
    """ICBI + Indo10Yr from PHEI (Penilai Harga Efek Indonesia)."""
    result = {}
    try:
        resp = fetch(
            "https://www.phei.co.id/en-us/Data/Fair-Prices-and-Yield", timeout=30
        )
        bs = BeautifulSoup(resp.text, "lxml")

        # ── ICBI from the header card ──
        icbi_el = bs.find(string="ICBI")
        if icbi_el:
            container = icbi_el.find_parent(class_="col-md-12")
            if not container:
                container = icbi_el.find_parent("div", class_=True)
                while container:
                    if has_css_class(container, "col-md-12"):
                        break
                    container = container.parent
                    if not container or getattr(container, "name", "") == "html":
                        container = None
                        break
            if container:
                text = container.get_text("|", strip=True)
                parts = text.split("|")
                # Expected: ICBI|▲|426.4080|Previous|425.7156|Change|0.6925|Change (%)|0.16
                if len(parts) >= 9:
                    close = parts[2]
                    prev = parts[4]
                    chg = parts[6]
                    pct = parts[8]
                    result["ICBI"] = {
                        "close": close,
                        "change": chg,
                        "change_pct": f"{pct}%",
                        "source": "PHEI",
                    }

        # ── Indo10Yr from IGSYC table ──
        tables = bs.find_all("table")
        for table in tables:
            rows = table.find_all("tr")
            if not rows:
                continue
            header = rows[0].find_all(["th", "td"])
            header_texts = [c.get_text(strip=True) for c in header]
            if "Tenor" not in " ".join(header_texts):
                continue
            for row in rows[1:]:
                cells = row.find_all("td")
                if len(cells) < 3:
                    continue
                tenor = cells[0].get_text(strip=True)
                if tenor == "10.0":
                    today = cells[1].get_text(strip=True)
                    yesterday = cells[2].get_text(strip=True)
                    try:
                        t = float(today)
                        y = float(yesterday)
                        chg = round(t - y, 4)
                        pct = round((chg / y) * 100, 2) if y else 0.0
                        result["Indo10Yr"] = {
                            "close": f"{t:.4f}",
                            "change": f"{chg:+.4f}",
                            "change_pct": f"{pct:+.2f}%",
                            "source": "PHEI",
                        }
                    except (ValueError, TypeError):
                        pass
                    break
            break  # only first tenor table
    except Exception as e:
        print(f"  WARN PHEI: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return result


def parse_commodities_futures():
    results = {}
    wanted = {
        "Crude Oil WTI": "Oil(WT)",
        "Brent Oil": "Oil(Brn)",
        "Natural Gas": "Ntrl Gas",
        "Gold": "Gold",
        "Silver": "Silver",
        "Copper": "Copper",
        "Aluminium": "Aluminium",
        "Nickel": "Nickel",
        "Tin": "Timah",
        "US Corn": "Corn",
        "US Soybean Oil": "SoybeanOil",
        "US Wheat": "Wheat",
    }
    try:
        resp = fetch("https://www.investing.com/commodities/real-time-futures")
        bs = BeautifulSoup(resp.text, "lxml")
        tables = bs.find_all("table")
        if not tables:
            return results

        for table in tables:
            rows = table.find_all("tr")
            if not rows:
                continue

            # Inspect header row to detect column indices
            header_cells = rows[0].find_all(["th", "td"]) if rows else []
            header_texts = [c.get_text(" ", strip=True).lower() for c in header_cells]

            def find_header_index(keywords):
                for i, h in enumerate(header_texts):
                    for kw in keywords:
                        if kw in h:
                            return i
                return None

            name_idx = find_header_index(
                ["name", "contract", "commodity", "instrument", "symbol"]
            ) or (1 if len(header_cells) > 1 else 0)
            last_idx = (
                find_header_index(["last", "price", "close"])
                or find_header_index(["ltd"])
                or 3
            )
            chg_idx = find_header_index(["change", "chg"]) or None
            pct_idx = find_header_index(["%", "change (%)", "chg%", "change%", "ch%"])

            def _normalize_name(n: str) -> str:
                s = n or ""
                s = s.strip()
                # remove parenthetical notes like (Jul 26) and similar
                s = re.sub(r"\(.*?\)", "", s)
                # remove month tokens like 'Jul 26' or 'August 26'
                s = re.sub(
                    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s*\d{1,2}\b",
                    "",
                    s,
                    flags=re.I,
                )
                # strip 'derived' suffix and extra whitespace
                s = re.sub(r"\s*derived$", "", s, flags=re.I).strip()
                s = re.sub(r"\s+", " ", s)
                return s

            for row in rows[1:]:
                cells = row.find_all("td")
                if not cells:
                    continue
                name_raw = (
                    cells[name_idx].get_text(" ", strip=True)
                    if name_idx < len(cells)
                    else cells[0].get_text(" ", strip=True)
                )
                name_norm = _normalize_name(name_raw)
                # lightweight debug trace to help diagnose missing matches
                logger.debug("Commodities row: raw=%r norm=%r", name_raw, name_norm)

                # find the best matching wanted key (robust substring/word match)
                matched_key = None
                name_l = name_norm.lower()
                for wk in wanted.keys():
                    if wk.lower() == name_l:
                        matched_key = wk
                        break
                if not matched_key:
                    for wk in wanted.keys():
                        if wk.lower() in name_l or name_l in wk.lower():
                            matched_key = wk
                            break
                if not matched_key:
                    # token overlap (require at least 3-char token to avoid spurious matches)
                    tokens = re.findall(r"\w{3,}", name_l)
                    for wk in wanted.keys():
                        wk_l = wk.lower()
                        for t in tokens:
                            if t in wk_l:
                                matched_key = wk
                                break
                        if matched_key:
                            break
                if not matched_key:
                    logger.debug("Commodities: no match for '%s'", name_norm)
                    continue

                # robustly locate numeric fields after the name cell
                last_txt = ""
                chg_txt = ""
                pct_txt = ""

                # attempt by header indices first
                if isinstance(last_idx, int) and last_idx < len(cells):
                    last_txt = cells[last_idx].get_text(strip=True)

                # fallback: find the first numeric-like cell after the name column
                if not last_txt:
                    for c in cells[name_idx + 1 :]:
                        t = c.get_text(strip=True)
                        if re.search(r"[0-9]", t) and re.search(r"\d", t):
                            # prefer values that look like a price (contains digit and dot or comma)
                            if re.match(r"^[+-]?\d[\d,\.]*%?$", t):
                                last_txt = t
                                break

                # change: try header index then first numeric after last that's different
                if chg_idx is not None and chg_idx < len(cells):
                    chg_txt = cells[chg_idx].get_text(strip=True)
                else:
                    for c in cells[name_idx + 1 :]:
                        t = c.get_text(strip=True)
                        if not t:
                            continue
                        if t == last_txt:
                            continue
                        if re.match(r"^[+-]?\d[\d,\.]*%?$", t):
                            chg_txt = t
                            break

                # percent: prefer explicit '%' occurrence from the end of the row
                if pct_idx is not None and pct_idx < len(cells):
                    pct_txt = cells[pct_idx].get_text(strip=True)
                else:
                    for c in reversed(cells):
                        t = c.get_text(strip=True)
                        if "%" in t:
                            pct_txt = t
                            break

                if not last_txt:
                    continue

                code = wanted[matched_key]
                logger.debug(
                    "Commodities: matched %r -> %r (code=%s)",
                    name_norm,
                    matched_key,
                    code,
                )
                results[code] = {
                    "close": clean_num(last_txt),
                    "change": clean_num(chg_txt) if chg_txt else None,
                    "change_pct": pct_txt,
                    "source": "Investing Futures",
                }
            # If some wanted items were not found in the tables (Investing may
            # render them via different tables or separate instrument pages),
            # attempt fallback to their instrument pages.
            missing_codes = [c for c in wanted.values() if c not in results]
            if missing_codes:
                fallback_urls = {
                    "Oil(WT)": [
                        "https://www.investing.com/commodities/crude-oil",
                        "https://www.investing.com/commodities/crude-oil-wti",
                    ],
                    "Oil(Brn)": [
                        "https://www.investing.com/commodities/brent-oil",
                        "https://www.investing.com/commodities/brent-oil-futures",
                    ],
                    "Ntrl Gas": [
                        "https://www.investing.com/commodities/natural-gas",
                        "https://www.investing.com/commodities/natural-gas-futures",
                    ],
                    "Aluminium": [
                        "https://www.investing.com/commodities/aluminium",
                        "https://www.investing.com/commodities/aluminum",
                    ],
                    "Nickel": [
                        "https://www.investing.com/commodities/nickel",
                    ],
                }
                for code in missing_codes:
                    urls = fallback_urls.get(code, [])
                    for url in urls:
                        try:
                            logger.debug(
                                "Commodities: fallback try %s for code %s", url, code
                            )
                            parsed = parse_instrument_page(
                                url, "Investing Futures", code
                            )
                            if parsed and isinstance(parsed, dict) and parsed.get(code):
                                val = parsed.get(code)
                                logger.debug(
                                    "Commodities: fallback parsed for %s: %s",
                                    code,
                                    bool(val),
                                )
                                if is_valid_data(val):
                                    results[code] = val
                                    break
                        except Exception as e:
                            logger.warning(
                                "Commodities fallback %s: %s: %s",
                                url,
                                type(e).__name__,
                                str(e)[:80],
                            )
                            # ignore and try next fallback URL
                            continue
    except Exception as e:
        print(f"  WARN Commodities: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return results


# ──────────────── STATE/JSON-BASED PARSERS ────────────────


def parse_instrument_page(url, label, code_name):
    result = {}
    try:
        resp = fetch(url)
        bs = BeautifulSoup(resp.text, "lxml")
        for script in bs.find_all("script"):
            if script.get("id") == "__NEXT_DATA__":
                if not script.string:
                    continue
                try:
                    data = json.loads(script.string)
                except Exception:
                    continue
                state = data["props"]["pageProps"]["state"]

                for store_key in [
                    "commodityStore",
                    "indexStore",
                    "bondStore",
                    "currencyStore",
                    "etfStore",
                    "equityStore",
                ]:
                    store = state.get(store_key, {})
                    instrument = store.get("instrument", {})
                    if not instrument:
                        continue
                    price = instrument.get("price", {})
                    if price and price.get("last") is not None:
                        result = {
                            "close": str(price["last"]),
                            "change": str(price.get("change", "")),
                            "change_pct": str(price.get("changePcr", "")),
                            "high": str(price.get("high", "")),
                            "low": str(price.get("low", "")),
                            "open": str(price.get("open", "")),
                            "prev_close": str(price.get("lastClose", "")),
                            "source": label,
                        }
                        break
                if not result:
                    quotes = state.get("quotesStore", {}).get("quotes", [])
                    if isinstance(quotes, list) and len(quotes) > 0:
                        q = quotes[0]
                        if q.get("last") is not None:
                            result = {
                                "close": str(q["last"]),
                                "change": str(q.get("change", "")),
                                "change_pct": str(q.get("changePct", "")),
                                "source": label,
                            }
                break
    except Exception as e:
        print(f"  WARN {label}: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return {code_name: result} if result else {}


# ──────────────── YAHOO FINANCE ────────────────


def parse_yahoo_finance(ticker, code_name):
    result = {}
    try:
        url = f"https://finance.yahoo.com/quote/{ticker}/"
        resp = fetch(url)
        bs = BeautifulSoup(resp.text, "lxml")
        qsp = bs.find("span", {"data-testid": "qsp-price"})
        price = qsp.get_text(strip=True) if qsp else None
        price = str(price) if price is not None else None

        if not price:
            price_el = bs.find(
                "fin-streamer",
                {"data-field": "regularMarketPrice", "data-symbol": ticker},
            )
            if not price_el:
                price_el = bs.find("fin-streamer", {"data-field": "regularMarketPrice"})
            price = (
                price_el.get("data-value") or price_el.get_text(strip=True)
                if price_el
                else None
            )
            price = str(price) if price is not None else None

        # First try: fin-streamer with matching data-symbol
        change_el = bs.find(
            "fin-streamer", {"data-field": "regularMarketChange", "data-symbol": ticker}
        )
        pct_el = bs.find(
            "fin-streamer",
            {"data-field": "regularMarketChangePercent", "data-symbol": ticker},
        )
        change = (
            change_el.get("data-value") or change_el.get_text(strip=True)
            if change_el
            else ""
        )
        change = str(change) if change is not None else ""
        pct = pct_el.get("data-value") or pct_el.get_text(strip=True) if pct_el else ""
        pct = str(pct) if pct is not None else ""

        # Second try: if fin-streamer not found for this ticker, parse from parent text
        if not change_el and qsp:
            parent_txt = qsp.parent.get_text(" ", strip=True) if qsp.parent else ""
            m = re.search(r"([+-]?\d+[\d.]*)\s*\(([+-]?\d+[\d.]*)%\)", parent_txt)
            if m:
                change = m.group(1)
                pct = m.group(2)
            else:
                m2 = re.search(r"([+-]?\d+[\d.]*)\s*\(([+-]?\d+[\d.]*)", parent_txt)
                if m2:
                    change = m2.group(1)
                    pct = m2.group(2)

        if price:
            price = str(price).replace(",", "")
            valid_change = change or ""
            valid_pct = f"{pct}%" if pct else ""
            if valid_change and price:
                try:
                    chg_num = abs(float(valid_change))
                    close_num = float(price)
                    if pct:
                        pct_num = abs(float(str(pct).rstrip("%")))
                        if pct_num < 1.0 and chg_num > close_num / 10:
                            valid_change = ""
                            valid_pct = ""
                except ValueError:
                    pass
            result = {
                code_name: {
                    "close": price,
                    "change": valid_change,
                    "change_pct": valid_pct,
                    "source": "Yahoo Finance",
                }
            }
    except Exception as e:
        print(
            f"  WARN {code_name} (Yahoo): {type(e).__name__}: {str(e)[:60]}",
            file=sys.stderr,
        )
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
    for name, ticker in sectors:
        try:
            parsed = parse_yahoo_finance(ticker, name)
            if parsed and isinstance(parsed, dict):
                result.update(parsed)
        except Exception as e:
            logger.debug("parse_yahoo_sector_indices %s: %s", ticker, e)

    return result


# ──────────────── BONDS ────────────────


def parse_indonesia_bonds():
    results = {}
    try:
        resp = fetch(
            "https://www.investing.com/rates-bonds/indonesia-government-bonds?"
            "maturity_from=40&maturity_to=290"
        )
        bs = BeautifulSoup(resp.text, "lxml")
        tables = bs.find_all("table")
        if tables:
            table = tables[0]
            rows = table.find_all("tr")
            for row in rows:
                cells = row.find_all("td")
                if len(cells) >= 5:
                    name = cells[1].get_text(" ", strip=True)
                    if "10Y" in name or "10 Yr" in name:
                        results["Indo10Yr"] = {
                            "close": cells[2].get_text(strip=True),
                            "prev": cells[3].get_text(strip=True),
                            "source": "Investing Bonds",
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
                "OBJ": {
                    "UNIT": "",
                    "DECIMAL": 2,
                    "UNIT_DELTA": "%",
                    "DECIMAL_DELTA": 2,
                },
                "COUNTRY1": {
                    "SYMBOL": "39",
                    "PAESE": "Indonesia",
                    "PAESE_UPPERCASE": "INDONESIA",
                    "BANDIERA": "id",
                    "URL_PAGE": "indonesia",
                },
                "COUNTRY2": None,
                "OBJ1": {"DURATA_STRING": "5 Years", "DURATA": 60},
                "OBJ2": None,
            }
        }
        resp = req.post(
            "https://www.worldgovernmentbonds.com/wp-json/common/v1/historical",
            json=payload,
            headers={
                "Origin": "https://www.worldgovernmentbonds.com",
                "Referer": "https://www.worldgovernmentbonds.com/cds-historical-data/indonesia/5-years/",
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/json",
            },
            impersonate=cast(Any, RETRY_IMPERSONATE[0]),
            timeout=20,
        )

        data = resp.json()
        if not data.get("success"):
            return result
        r = data["result"]
        close = str(r["ultimoValore"])
        change = ""
        change_pct = ""
        html = r.get("htmlLatestChange", "")
        if html:
            soup = BeautifulSoup(html, "lxml")
            for tr in soup.find_all("tr"):
                cells = tr.find_all("td")
                if len(cells) >= 5 and "1 Week" in cells[0].get_text(strip=True):
                    min_div = cells[2].find("div")
                    prev_val = min_div.get_text(strip=True) if min_div else ""
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
        result["IndoCDS 5yr"] = {
            "close": close,
            "change": change,
            "change_pct": change_pct,
            "source": "WorldGovernmentBonds",
        }
    except Exception as e:
        print(f"  WARN IndoCDS: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return result


# ──────────────── AMMONIA ────────────────


def parse_ammonia():
    result = {}
    try:
        resp = fetch(
            "https://www.chemicalbook.com/PriceInfoall_CB9854275.htm",
            headers=ZH_HEADERS,
            timeout=15,
        )
        soup = BeautifulSoup(resp.text, "lxml")
        entries = []
        for li in soup.find_all("li"):
            if has_css_class(li, "align_r"):
                continue
            txt = li.get_text(" ", strip=True)
            m = re.search(r"(\d+月\d+日).*?氨.*?报价[:：]?(\d[\d,.]*)", txt)
            if m:
                entries.append(
                    {
                        "date": m.group(1),
                        "price": m.group(2).replace(",", ""),
                    }
                )
        if entries:
            latest = entries[0]
            close = latest["price"]
            change = ""
            change_pct = ""
            if len(entries) >= 2:
                prev = entries[1]
                try:
                    c = float(close)
                    p = float(prev["price"])
                    diff = round(c - p, 2)
                    pc = round((diff / p) * 100, 2) if p != 0 else 0
                    change = f"+{diff}" if diff >= 0 else str(diff)
                    change_pct = f"+{pc}%" if pc >= 0 else f"{pc}%"
                except (ValueError, TypeError):
                    pass
            result["Ammonia"] = {
                "close": close,
                "change": change,
                "change_pct": change_pct,
                "date": latest["date"],
                "unit": "Yuan/ton",
                "note": f"ChemicalBook ({entries[0]['date']})",
                "source": "ChemicalBook",
            }
    except Exception as e:
        print(f"  WARN Ammonia: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return result


# ──────────────── JISDOR ────────────────


def parse_jisdor():
    result = {}
    try:
        resp = fetch(
            "https://www.bi.go.id/id/statistik/informasi-kurs/jisdor/default.aspx",
            timeout=25,
        )
        soup = BeautifulSoup(resp.text, "lxml")
        rates = []
        for td in soup.find_all("td"):
            txt = td.get_text(strip=True)
            m = re.match(r"Rp(\d{2,3}\.\d{3})[,\s]", txt)
            if m:
                rates.append(m.group(1).replace(".", ","))
        if len(rates) >= 2:
            curr = rates[0].replace(",", "")
            prev = rates[1].replace(",", "")
            curr_f = float(curr)
            prev_f = float(prev)
            change = round(curr_f - prev_f, 0)
            change_pct = round(((curr_f - prev_f) / prev_f) * 100, 2)
            result["Jisdor"] = {
                "close": rates[0],
                "change": f"{change:+.0f}",
                "change_pct": f"{change_pct:+.2f}%",
                "source": "BI",
            }
        elif len(rates) == 1:
            result["Jisdor"] = {
                "close": rates[0],
                "change": "",
                "change_pct": "",
                "source": "BI",
            }
    except Exception as e:
        print(f"  WARN JISDOR: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return result


# ──────────────── DXY from Yahoo Finance API ────────────────


def parse_yahoo_dxy():
    """Fetch DXY via Yahoo Finance v8 chart API (more reliable than HTML scraping)."""
    result = {}
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?interval=1d&range=1d"
        resp = fetch(url, timeout=20)
        data = resp.json()
        meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        prev_close = meta.get("chartPreviousClose")
        if price and prev_close:
            change = round(price - prev_close, 3)
            pct = round(((price - prev_close) / prev_close) * 100, 2)
            result["USDIndx"] = {
                "close": str(price),
                "change": f"{change:+.3f}",
                "change_pct": f"{pct:+.2f}%",
                "source": "Yahoo Finance API",
            }
        elif price:
            result["USDIndx"] = {
                "close": str(price),
                "change": "",
                "change_pct": "",
                "source": "Yahoo Finance API",
            }
    except Exception as e:
        print(
            f"  WARN DXY (Yahoo API): {type(e).__name__}: {str(e)[:60]}",
            file=sys.stderr,
        )
    return result


# ──────────────── IDX Property from Yahoo Finance API ────────────────


def parse_yahoo_idx_property():
    """Fetch IDX Property (IDXPROPERT.JK) via Yahoo Finance v8 chart API."""
    result = {}
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/IDXPROPERT.JK?interval=1d&range=5d"
        resp = fetch(url, timeout=20)
        data = resp.json()
        meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        prev_close = meta.get("chartPreviousClose")
        if price and prev_close:
            change = round(price - prev_close, 2)
            pct = round(((price - prev_close) / prev_close) * 100, 2)
            result["IDX Property"] = {
                "close": str(price),
                "change": f"{change:+.2f}",
                "change_pct": f"{pct:+.2f}%",
                "source": "Yahoo Finance API",
            }
        elif price:
            result["IDX Property"] = {
                "close": str(price),
                "change": "",
                "change_pct": "",
                "source": "Yahoo Finance API",
            }
    except Exception as e:
        print(
            f"  WARN IDX Property (Yahoo API): {type(e).__name__}: {str(e)[:60]}",
            file=sys.stderr,
        )
    return result


# ──────────────── BAR CHART COAL ────────────────


def parse_barchart_coal():
    result = {}
    month_codes = {
        "Jun": "M",
        "Jul": "N",
        "Aug": "Q",
        "Sep": "U",
    }

    for root_name, root_sym, label in [
        ("Newcastle", "LQ", "Coal(Nwl)"),
        ("Rotterdam", "LU", "Coal(Rot)"),
    ]:
        contracts = []
        for month_name, code in month_codes.items():
            sym = f"{root_sym}{code}26"

            found_row = False
            try:
                resp = fetch(
                    f"https://www.barchart.com/futures/quotes/{sym}/overview",
                    timeout=20,
                )
                soup = BeautifulSoup(resp.text, "lxml")
                tables = soup.find_all("table")
                for table in tables:
                    rows = table.find_all("tr")
                    if not rows or len(rows) <= 1:
                        continue
                    # Search all data rows for a matching symbol
                    for row in rows[1:]:
                        cells = row.find_all("td")
                        if len(cells) < 3:
                            continue
                        if cells[0].get_text(strip=True) != sym:
                            continue
                        price_raw = cells[1].get_text(" ", strip=True)
                        chg_raw = cells[2].get_text(" ", strip=True)
                        # normalize common noise
                        price_raw = price_raw.replace("s", "").replace(",", "")
                        chg_raw = chg_raw.replace(",", "")

                        price_str, change_str, change_pct = parse_barchart_price_change(
                            price_raw, chg_raw
                        )

                        if price_str or change_str:
                            contracts.append(
                                {
                                    "month": month_name,
                                    "price": price_str,
                                    "change": change_str,
                                    "change_pct": change_pct,
                                }
                            )
                        found_row = True
                        break
                    if found_row:
                        break
            except Exception as e:
                logger.debug("parse_barchart_coal page exception for %s: %s", sym, e)

        if contracts:
            result[label] = {
                "contracts": contracts,
                "source": "Barchart",
            }

    return result


def parse_bursa_cpo():
    """FCPO from Bursa Malaysia derivatives market table, row 3."""
    result = {}
    try:
        resp = fetch(
            "https://www.bursamalaysia.com/trade/market/derivatives_market",
            timeout=30,
        )
        bs = BeautifulSoup(resp.text, "lxml")
        tables = bs.find_all("table")
        for table in tables:
            rows = table.find_all("tr")
            if len(rows) < 4:
                continue
            # Find the FCPO table: first header cell should say "Futures/Months"
            header_cells = rows[0].find_all(["th", "td"])
            if not header_cells or "Futures" not in header_cells[0].get_text(
                strip=True
            ):
                continue
            # Row index 2 = 3rd row (0-based), the row the user wants
            row = rows[2]
            cells = row.find_all(["td", "th"])
            if len(cells) < 4:
                continue
            last_raw = cells[1].get_text(strip=True).replace(",", "")
            chg_raw = cells[2].get_text(strip=True).replace(",", "")
            try:
                close = float(last_raw)
                change = float(chg_raw)
                prev_close = close - change
                pct = round((change / prev_close) * 100, 2) if prev_close else 0.0
                result["CPO"] = {
                    "close": f"{close:.2f}",
                    "change": f"{change:+.2f}",
                    "change_pct": f"{pct:+.2f}%",
                    "source": "Bursa Malaysia",
                }
            except (ValueError, TypeError):
                pass
            break
    except Exception as e:
        print(f"  WARN Bursa CPO: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return result


# ──────────────── SUNSIRS WOOD PULP ────────────────


def parse_sunsirs_woodpulp():
    """Wood pulp spot price from SunSirs Daily table (Building materials sector)."""
    result = {}
    try:
        resp = fetch("https://www.sunsirs.com/uk/sectors-17.html")
        bs = BeautifulSoup(resp.text, "lxml")
        tables = bs.find_all("table")
        if not tables:
            return result
        # First table = Spot Price: Daily
        table = tables[0]
        rows = table.find_all("tr")
        for row in rows[1:]:  # skip header
            cells = row.find_all("td")
            if len(cells) < 5:
                continue
            name = cells[0].get_text(strip=True)
            if "Wood pulp" in name:
                prev_raw = cells[2].get_text(strip=True).replace(",", "")
                close_raw = cells[3].get_text(strip=True).replace(",", "")
                pct_raw = (
                    cells[4].get_text(strip=True).replace("%", "").replace(",", "")
                )
                try:
                    close = float(close_raw)
                    prev = float(prev_raw)
                    change = close - prev
                    pct = float(pct_raw)
                    result["Woodpulp"] = {
                        "close": f"{close:.2f}",
                        "change": f"{change:+.2f}",
                        "change_pct": f"{pct:+.2f}%",
                        "source": "SunSirs",
                    }
                except (ValueError, TypeError):
                    pass
                break
    except Exception as e:
        print(
            f"  WARN SunSirs Woodpulp: {type(e).__name__}: {str(e)[:60]}",
            file=sys.stderr,
        )
    return result


# ──────────────────── DATA COLLECTION ────────────────────


def collect_data():
    """Run all scrapers, return (data_dict, sources_list, timestamp)."""
    DATA = {}

    def log(msg):
        print(msg, file=sys.stderr, flush=True)

    def run_task(label, fn):
        try:
            return fn()
        except Exception as e:
            log(f"  WARN {label}: {type(e).__name__}: {str(e)[:80]}")
            return {}

    log("Regional Screener -- collecting data...")
    t0 = datetime.now()

    single_pages = [
        (
            "Iron Ore",
            "https://www.investing.com/commodities/iron-ore-62-cfr-futures",
            "Iron Ore 62%",
        ),
        ("Tin", "https://www.investing.com/commodities/tin", "Timah"),
        ("Silver", "https://www.investing.com/commodities/silver", "Silver"),
        ("Copper", "https://www.investing.com/commodities/copper", "Copper"),
        (
            "BCOMIN",
            "https://www.investing.com/indices/bloomberg-industrial-metals",
            "BCOMIN",
        ),
        ("USD/IDR", "https://www.investing.com/currencies/usd-idr", "IDR"),
        ("EUR/USD", "https://www.investing.com/currencies/eur-usd", "Euro"),
        ("Gold Spot", "https://www.investing.com/currencies/xau-usd", "Gold(Spot)"),
        # Additional direct instrument pages to ensure key commodities are fetched
        ("Crude Oil WTI", "https://www.investing.com/commodities/crude-oil", "Oil(WT)"),
        ("Brent Oil", "https://www.investing.com/commodities/brent-oil", "Oil(Brn)"),
        (
            "Natural Gas",
            "https://www.investing.com/commodities/natural-gas",
            "Ntrl Gas",
        ),
        ("Nickel", "https://www.investing.com/commodities/nickel", "Nickel"),
        ("Aluminium", "https://www.investing.com/commodities/aluminium", "Aluminium"),
    ]
    tasks = [
        ("Coal from Barchart", parse_barchart_coal),
        ("IDX Sector Indices", parse_yahoo_sector_indices),
        ("JISDOR", parse_jisdor),
        (
            "Major Indices",
            lambda: parse_table_pages(
                [
                    (
                        "Major Indices",
                        "https://www.investing.com/indices/major-indices",
                        1,
                        2,
                        5,
                        6,
                    ),
                ]
            ),
        ),
        (
            "IDX Indices",
            lambda: parse_table_pages(
                [
                    (
                        "IDX Indices",
                        "https://www.investing.com/indices/indonesia-indices?include-major-indices=true&include-additional-indices=true&include-primary-sectors=true&include-other-indices=true",
                        1,
                        2,
                        5,
                        6,
                    ),
                ]
            ),
        ),
        ("SunSirs Woodpulp", parse_sunsirs_woodpulp),
        ("Commodities", parse_commodities_futures),
        *[
            (
                label,
                lambda url=url, label=label, code=code: parse_instrument_page(
                    url, label, code
                ),
            )
            for label, url, code in single_pages
        ],
        (
            "US Bonds",
            lambda: parse_table_pages(
                [
                    (
                        "US Bonds",
                        "https://www.investing.com/rates-bonds/usa-government-bonds",
                        1,
                        2,
                        6,
                        7,
                    ),
                ]
            ),
        ),
        ("Indo Bonds", parse_indonesia_bonds),
        ("PHEI (ICBI + Indo10Yr)", parse_phei),
        *[
            (code, lambda ticker=ticker, code=code: parse_yahoo_finance(ticker, code))
            for ticker, code in [
                ("^VIX", "VIX"),
                ("EIDO", "EIDO"),
                ("EEM", "EEM"),
                ("TLK", "TLKM"),
                ("SI%3DF", "Silver"),
                ("HG%3DF", "Copper"),
            ]
        ],
        ("DXY Yahoo API", parse_yahoo_dxy),
        ("IndoCDS", parse_indonesia_cds),
        ("Ammonia", parse_ammonia),
        ("IDX Property", parse_yahoo_idx_property),
        ("Bursa CPO", parse_bursa_cpo),
    ]

    log(f"Submitting {len(tasks)} scraper tasks with {MAX_FETCH_WORKERS} workers...")
    results_by_index = {}
    with ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS) as executor:
        futures = {
            executor.submit(run_task, label, fn): idx
            for idx, (label, fn) in enumerate(tasks)
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                res = future.result()
            except Exception as e:
                res = {}
                log(f"  WARN task {idx}: {type(e).__name__}: {str(e)[:80]}")
            results_by_index[idx] = res
            label = tasks[idx][0]
            log(f"  done: {label}")

    for idx in range(len(tasks)):
        DATA.update(results_by_index.get(idx, {}))

    elapsed = (datetime.now() - t0).total_seconds()
    sources = sorted(
        set(v.get("source", "unknown") for v in DATA.values() if isinstance(v, dict))
    )

    log(f"\nDone in {elapsed:.1f}s -- {len(DATA)} items collected")
    ts = datetime.now().isoformat()
    # stamp per-key fetched_at if parsers didn't provide one
    for v in DATA.values():
        if isinstance(v, dict) and not v.get("fetched_at"):
            v["fetched_at"] = ts
    return DATA, sources, ts


# ──────────────────── MARKET NEWS ────────────────────


def fetch_market_news(max_items=5):
    """Fetch latest US market news headlines from Google News RSS."""
    news = []
    urls = [
        "https://news.google.com/rss/search?q=US+stock+market&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search?q=Wall+Street&hl=en-US&gl=US&ceid=US:en",
    ]
    seen_titles = set()
    for url in urls:
        try:
            r = fetch(url, timeout=15)
            text = r.text
            root = ET.fromstring(text)
            for item in root.iter("item"):
                title = item.findtext("title", "")
                link = item.findtext("link", "")
                if not title or not link:
                    continue
                title = title.split(" - ")[0].strip()
                key = title.lower()[:60]
                if key in seen_titles:
                    continue
                seen_titles.add(key)
                news.append({"title": title, "url": link})
                if len(news) >= max_items:
                    return news
        except Exception:
            pass
    return news


# ──────────────────── FORMATTING HELPERS ────────────────────
