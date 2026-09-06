"""Market news parser from Google News RSS."""

import xml.etree.ElementTree as ET
from .base import fetch


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
