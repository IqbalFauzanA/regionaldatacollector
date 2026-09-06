"""Investing.com scrapers for futures tables and instrument pages."""

import json
import logging
import re
import sys
from bs4 import BeautifulSoup
from regional_report.commons import clean_num, is_valid_data
from .base import fetch, resolve_parser
from .bloomberg import _parse_bloomberg_quote

logger = logging.getLogger(__name__)


def parse_commodities_futures():
    results = {}
    wanted_names = {
        "crude oil wti": "Oil WTI",
        "wti crude oil": "Oil WTI",
        "brent oil": "Oil Brent",
        "natural gas": "Nat Gas",
        "aluminium": "Aluminium",
        "aluminum": "Aluminium",
        "nickel": "Nickel",
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

            def normalize_header(value: str) -> str:
                value = value.lower().replace(".", " ")
                return re.sub(r"\s+", " ", value).strip()

            normalized_headers = [normalize_header(h) for h in header_texts]

            def find_header_index(accepted):
                return next(
                    (i for i, h in enumerate(normalized_headers) if h in accepted),
                    None,
                )

            name_idx = find_header_index(
                {"name", "contract", "commodity", "instrument", "symbol"}
            )
            last_idx = find_header_index({"last", "price", "close", "ltd"})
            pct_idx = next(
                (i for i, h in enumerate(normalized_headers) if "%" in h), None
            )
            chg_idx = next(
                (
                    i
                    for i, h in enumerate(normalized_headers)
                    if h in {"change", "chg", "change value", "chg value"}
                    and "%" not in h
                ),
                None,
            )

            # A compact sidebar table only has Last and Chg. %, so it cannot
            # supply the absolute move. Accept only the full futures table;
            # missing instruments are handled by the instrument-page fallback.
            if (
                name_idx is None
                or last_idx is None
                or chg_idx is None
                or pct_idx is None
            ):
                continue

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
                s = re.sub(r"\s+futures?$", "", s, flags=re.I).strip()
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

                # Use exact canonical names. Fuzzy token matching allowed rows such
                # as Soybean Oil and Dutch TTF Natural Gas to overwrite WTI and
                # Henry Hub Natural Gas respectively.
                canonical_name = re.sub(
                    r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", name_norm.lower())
                ).strip()
                report_label = wanted_names.get(canonical_name)
                if report_label is None:
                    logger.debug("Commodities: no match for '%s'", name_norm)
                    continue

                if max(last_idx, chg_idx, pct_idx) >= len(cells):
                    continue
                last_txt = cells[last_idx].get_text(strip=True)
                chg_txt = cells[chg_idx].get_text(strip=True)
                pct_txt = cells[pct_idx].get_text(strip=True)

                if not last_txt:
                    continue

                logger.debug(
                    "Commodities: matched %r (canonical=%r, label=%s)",
                    name_norm,
                    canonical_name,
                    report_label,
                )
                results[report_label] = {
                    "close": clean_num(last_txt),
                    "change": clean_num(chg_txt) if chg_txt else None,
                    "change_pct": pct_txt,
                    "source": "Investing Futures",
                }
        # Only fall back after every table has been searched. Doing this inside
        # the table loop repeated the same network requests for multi-table pages.
        fallback_urls = {
            "Oil WTI": [
                "https://www.bloomberg.com/quote/CL1:COM",
            ],
            "Oil Brent": [
                "https://www.bloomberg.com/quote/CO1:COM",
            ],
            "Nat Gas": [
                "https://www.investing.com/commodities/natural-gas",
                "https://www.investing.com/commodities/natural-gas-futures",
            ],
            "Aluminium": [
                "https://www.investing.com/commodities/aluminium",
                "https://www.investing.com/commodities/aluminum",
            ],
            "Nickel": ["https://www.investing.com/commodities/nickel"],
        }
        wanted_labels = dict.fromkeys(wanted_names.values())
        inst_parser = resolve_parser("parse_instrument_page", parse_instrument_page)
        for report_label in (label for label in wanted_labels if label not in results):
            for url in fallback_urls[report_label]:
                logger.debug(
                    "Commodities: fallback try %s for label %s", url, report_label
                )
                parsed = inst_parser(url, "Investing Futures", report_label)
                value = parsed.get(report_label)
                if is_valid_data(value):
                    results[report_label] = value
                    break
    except Exception as e:
        print(f"  WARN Commodities: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return results


def parse_instrument_page(url, source_label, report_label):
    if "bloomberg.com/quote/" in url:
        ticker = url.split("/quote/")[1].split("?")[0].strip("/")
        return _parse_bloomberg_quote(url, ticker, report_label)
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
                            "source": source_label,
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
                                "source": source_label,
                            }
                break
    except Exception as e:
        print(
            f"  WARN {source_label}: {type(e).__name__}: {str(e)[:60]}",
            file=sys.stderr,
        )
    return {report_label: result} if result else {}
