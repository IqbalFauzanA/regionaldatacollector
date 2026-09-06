"""Bursa Malaysia CPO derivatives scraper."""

import sys
from bs4 import BeautifulSoup
from .base import fetch


def parse_bursa_cpo():
    """FCPO Day (T), third displayed row, using its Last Done value."""
    page_url = "https://www.bursamalaysia.com/market_information/derivatives_prices"
    api_url = (
        "https://www.bursamalaysia.com/api/v1/derivatives_prices/"
        "derivatives_prices?code=FCPO&ses=day&per_page=20&page=1"
    )
    try:
        response = fetch(
            api_url,
            headers={"Referer": page_url},
            timeout=30,
        )
        return parse_bursa_cpo_payload(response.json())
    except Exception as e:
        print(f"  WARN Bursa CPO: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
        return {}


def parse_bursa_cpo_payload(payload):
    """Parse Bursa's DataTables JSON; columns follow the page's visible table."""
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list) or len(rows) < 3:
        return {}
    row = rows[2]  # third displayed FCPO Day (T) row
    if not isinstance(row, list) or len(row) < 8:
        return {}
    name = BeautifulSoup(str(row[1]), "lxml").get_text(" ", strip=True)
    if name != "FCPO":
        return {}
    try:
        close = float(str(row[6]).replace(",", ""))  # Last Done
        change_text = BeautifulSoup(str(row[7]), "lxml").get_text(" ", strip=True)
        change = float(change_text.replace(",", ""))
    except (TypeError, ValueError):
        return {}
    previous_close = close - change
    percent = (change / previous_close * 100) if previous_close else 0.0
    return {
        "CPO": {
            "close": f"{close:.2f}",
            "change": f"{change:+.2f}",
            "change_pct": f"{percent:+.2f}%",
            "contract": str(row[2]),
            "source": "Bursa Malaysia",
        }
    }
