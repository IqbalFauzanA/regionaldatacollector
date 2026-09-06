"""Indonesia domestic bond, CDS, PHEI, and JISDOR scrapers."""

import re
import sys
from datetime import datetime, timedelta
from typing import Any, cast
from bs4 import BeautifulSoup
from regional_report.commons import (
    RETRY_IMPERSONATE,
    clean_num,
    has_css_class,
    req,
)
from .base import fetch


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
                # Expected: ICBI|arrow|426.4080|Previous|425.7156|Change|0.6925|Change (%)|0.16
                # PHEI displays Change as an unsigned magnitude and uses the
                # arrow/color for direction, so compute the signed value from
                # the current and previous levels.
                if len(parts) >= 9:
                    close_raw = parts[2]
                    prev_raw = parts[4]
                    chg_raw = parts[6]
                    pct_raw = parts[8]

                    def _to_float(value):
                        cleaned = clean_num(str(value).replace("%", ""))
                        if cleaned is None:
                            return None
                        return float(cleaned)

                    try:
                        close_num = _to_float(close_raw)
                        prev_num = _to_float(prev_raw)
                    except (ValueError, TypeError):
                        close_num = None
                        prev_num = None

                    if close_num is not None and prev_num is not None:
                        chg_num = round(close_num - prev_num, 4)
                        if abs(chg_num) < 0.00005:
                            chg_num = 0.0
                        pct_num = (
                            round((chg_num / prev_num) * 100, 2) if prev_num else 0.0
                        )
                        result["ICBI"] = {
                            "close": f"{close_num:.4f}",
                            "change": f"{chg_num:+.4f}",
                            "change_pct": f"{pct_num:+.2f}%",
                            "source": "PHEI",
                        }
                    else:
                        result["ICBI"] = {
                            "close": close_raw,
                            "change": clean_num(chg_raw),
                            "change_pct": f"{clean_num(pct_raw)}%",
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


def _previous_business_day(value):
    previous = value - timedelta(days=1)
    while previous.weekday() >= 5:
        previous -= timedelta(days=1)
    return previous


def parse_indonesia_cds_payload(data, cached_item=None):
    """Parse Indonesia 5Y CDS and compare it with the cached prior weekday."""
    if not isinstance(data, dict) or not data.get("success"):
        return {}
    result = data.get("result", {})
    quotes = result.get("quote", {}) if isinstance(result, dict) else {}
    if not isinstance(quotes, dict):
        return {}

    # Keep the last weekday observation for each date.
    by_date = {}
    for quote in quotes.values():
        if not isinstance(quote, dict):
            continue
        date = str(quote.get("DATA_VAL", "")).strip()
        try:
            close_val = quote.get("CLOSE_VAL")
            if not isinstance(close_val, (int, float, str)):
                continue
            value = float(close_val)
        except (TypeError, ValueError):
            continue
        if date:
            try:
                quote_date = datetime.strptime(date, "%Y-%m-%d").date()
            except ValueError:
                continue
            # The source can repeat Friday's value across the weekend.
            if quote_date.weekday() < 5:
                by_date[quote_date] = value
    dates = sorted(by_date)
    if not dates:
        return {}

    latest_day = dates[-1]
    latest_date = latest_day.isoformat()
    latest = by_date[latest_day]
    close = f"{latest:.2f}"
    change = ""
    change_pct = ""
    previous_close = ""
    previous_date = ""
    if isinstance(cached_item, dict):
        cached_date = str(cached_item.get("date", ""))
        if (
            cached_date == latest_date
            and cached_item.get("source") == "WorldGovernmentBonds"
        ):
            # Preserve the move on same-market-day reruns.
            change = str(cached_item.get("change", ""))
            change_pct = str(cached_item.get("change_pct", ""))
            previous_close = str(cached_item.get("previous_close", ""))
            previous_date = str(cached_item.get("previous_date", ""))
        elif cached_date == _previous_business_day(latest_day).isoformat():
            try:
                previous = float(str(cached_item.get("close", "")).replace(",", ""))
            except (TypeError, ValueError):
                previous = None
            if previous is not None:
                difference = latest - previous
                percent = (difference / previous * 100) if previous else 0.0
                previous_close = f"{previous:.2f}"
                previous_date = cached_date
                change = f"{difference:+.2f}"
                change_pct = f"{percent:+.2f}%"

    return {
        "IndoCDS 5yr": {
            "close": close,
            "change": change,
            "change_pct": change_pct,
            "date": latest_date,
            "previous_close": previous_close,
            "previous_date": previous_date,
            "source": "WorldGovernmentBonds",
        }
    }


def parse_indonesia_cds(cached_item=None):
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

        return parse_indonesia_cds_payload(resp.json(), cached_item=cached_item)
    except Exception as e:
        print(f"  WARN IndoCDS: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
        return {}


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
                rates.append(m.group(1).replace(".", ""))
        if len(rates) >= 2:
            curr_f = float(rates[0])
            prev_f = float(rates[1])
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
