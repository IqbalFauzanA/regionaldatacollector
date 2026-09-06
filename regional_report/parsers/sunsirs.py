"""SunSirs commodity scrapers for ammonia and woodpulp."""

import sys
from bs4 import BeautifulSoup
from .base import fetch

SUNSIRS_AMMONIA_URL = (
    "https://www.sunsirs.com/m/page/commodity-price-detail/"
    "commodity-price-detail-965.html"
)


def parse_sunsirs_ammonia_html(html):
    """Parse SunSirs' seven-day China liquid-ammonia price series."""
    soup = BeautifulSoup(html, "lxml")
    entries = []
    for row in soup.select("li.zwd_table_li"):
        cells = [item.get_text(" ", strip=True) for item in row.find_all("p")]
        if len(cells) < 3 or cells[0].lower() != "liquid ammonia":
            continue
        try:
            price = float(cells[1].replace(",", ""))
        except ValueError:
            continue
        entries.append({"price": price, "date": cells[2]})

    if not entries:
        return {}
    latest = entries[0]
    change = ""
    change_pct = ""
    previous = next(
        (entry for entry in entries[1:] if entry["date"] != latest["date"]),
        None,
    )
    if previous:
        previous_price = previous["price"]
        difference = latest["price"] - previous_price
        percent = (difference / previous_price * 100) if previous_price else 0.0
        change = f"{difference:+.2f}"
        change_pct = f"{percent:+.2f}%"

    return {
        "Ammonia": {
            "close": f"{latest['price']:.2f}",
            "change": change,
            "change_pct": change_pct,
            "date": latest["date"],
            "previous_date": previous["date"] if previous else "",
            "unit": "RMB/ton",
            "note": f"SunSirs ({latest['date']})",
            "source": "SunSirs",
        }
    }


def parse_ammonia():
    try:
        return parse_sunsirs_ammonia_html(fetch(SUNSIRS_AMMONIA_URL, timeout=30).text)
    except Exception as e:
        print(f"  WARN Ammonia: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
        return {}


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
        headers = rows[0].find_all(["th", "td"]) if rows else []
        previous_date = headers[2].get_text(strip=True) if len(headers) > 3 else ""
        latest_date = headers[3].get_text(strip=True) if len(headers) > 3 else ""
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
                        "date": latest_date,
                        "previous_date": previous_date,
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
