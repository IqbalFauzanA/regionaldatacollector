"""Base parsing utilities and dynamic fetch dispatcher."""

import logging
import re
import sys
from bs4 import BeautifulSoup
from regional_report.commons import clean_num
from regional_report.commons import fetch as _commons_fetch

logger = logging.getLogger(__name__)


def fetch(*args, **kwargs):
    """Dynamic fetch dispatcher.

    Allows tests patching 'regional_report.parsers.fetch' to intercept network
    calls across all submodules.
    """
    parsers_mod = sys.modules.get("regional_report.parsers")
    if parsers_mod and hasattr(parsers_mod, "fetch") and parsers_mod.fetch is not fetch:
        return parsers_mod.fetch(*args, **kwargs)
    return _commons_fetch(*args, **kwargs)


def resolve_parser(name, default_fn):
    """Resolve parser from package namespace if patched, else return default."""
    parsers_mod = sys.modules.get("regional_report.parsers")
    if parsers_mod and hasattr(parsers_mod, name):
        target = getattr(parsers_mod, name)
        if target is not default_fn:
            return target
    return default_fn


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
        return float(m.group(0).replace(",", ""))

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
        pct = round((chg_num / prev_close) * 100, 2) if prev_close else 0.0

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


def label_from_name(name):
    mapping = {
        "dow jones": "Dow",
        "s&p 500": "S&P 500",
        "nasdaq": "Nasdaq",
        "ftse 100": "FTSE",
        "dax": "DAX",
        "cac 40": "CAC",
        "nikkei 225": "Nikkei 225",
        "hang seng": "HSI",
        "shanghai": "Shanghai",
        "idx composite": "IDX",
        "idx lq45": "LQ45",
        "idx30": "IDX30",
        "idx 30": "IDX30",
        "idx kompas 100": "Kompas 100",
        "u.s. 2y": "US2Yr",
        "u.s. 10y": "US10Yr",
        "u.s. 30y": "US30Yr",
        "s&p 500 vix": "S&P 500 VIX",
    }
    key = name.lower().strip()
    if key in mapping:
        return mapping[key]
    return name


def parse_table_pages(pages, requested_keys):
    """Parse table rows and retain only report-requested keys."""
    requested_keys = set(requested_keys)
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
                    report_label = label_from_name(name_clean)
                    if report_label not in requested_keys:
                        continue
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
                    if last_txt and report_label:
                        results[report_label] = {
                            "close": clean_num(last_txt),
                            "change": clean_num(chg_txt),
                            "change_pct": pct_txt,
                            "source": label,
                        }
        except Exception as e:
            print(f"  WARN {label}: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return results
