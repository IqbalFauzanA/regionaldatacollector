"""Shared configuration, paths, HTTP client, and utility helpers."""

import logging
import os
import re
import sys
import time
from datetime import datetime
from threading import BoundedSemaphore
from typing import Any, cast
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Prefer curl_cffi for faster, modern TLS handling. Fall back to requests
# when curl_cffi isn't installed (useful in dev environments).
try:
    import curl_cffi.requests as req
except Exception:
    import requests as _requests

    class _ReqAdapter:
        def __init__(self):
            self._session = _requests.Session()

        def get(self, url, impersonate=None, timeout=15, headers=None, **kwargs):
            hdrs = dict(headers or {})
            if impersonate and "User-Agent" not in hdrs:
                hdrs["User-Agent"] = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            return self._session.get(url, timeout=timeout, headers=hdrs, **kwargs)

        def post(
            self,
            url,
            data=None,
            json=None,
            impersonate=None,
            timeout=15,
            headers=None,
            **kwargs,
        ):
            hdrs = dict(headers or {})
            if impersonate and "User-Agent" not in hdrs:
                hdrs["User-Agent"] = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            return self._session.post(
                url, data=data, json=json, timeout=timeout, headers=hdrs, **kwargs
            )

    req = _ReqAdapter()


MAX_FETCH_WORKERS = 8
CACHE_MAX_AGE_SECONDS = 3600
CACHE_MAX_CLOCK_SKEW_SECONDS = 300

RETRY_IMPERSONATE = [
    "chrome120",
    "chrome119",
    "chrome",
    "firefox",
    "safari",
    "chrome_android",
    "safari_ios",
    "edge101",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
ZH_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

HOST_LIMITS = {
    "www.investing.com": 3,
    "query1.finance.yahoo.com": 4,
    "finance.yahoo.com": 4,
    "www.barchart.com": 2,
    "www.bloomberg.com": 1,
}


def _resolve_base_dir() -> str:
    if getattr(sys, "frozen", False):
        import tempfile

        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        cwd = os.getcwd()
        tempdir = tempfile.gettempdir()
        try:
            if os.path.commonpath([exe_dir, tempdir]) == tempdir or exe_dir.startswith(
                tempdir
            ):
                return cwd
            return exe_dir
        except Exception:
            return cwd

    # commons.py lives in regional_report/, while cache/output/fonts live at repo root.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE_DIR = _resolve_base_dir()
CACHE_DIR = os.path.join(BASE_DIR, "cache")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
CACHE_JSON = os.path.join(CACHE_DIR, "regional_raw.json")
REPORT_PDF = os.path.join(OUTPUT_DIR, "regional_report.pdf")
REPORT_WA = os.path.join(OUTPUT_DIR, "regional_report_whatsapp.txt")

_HOST_SEMAPHORES = {}


def _get_host_semaphore(url: str):
    host = urlparse(url).netloc.lower()
    limit = HOST_LIMITS.get(host, 3)
    sem = _HOST_SEMAPHORES.get(host)
    if sem is None:
        sem = BoundedSemaphore(limit)
        _HOST_SEMAPHORES[host] = sem
    return sem


def fetch(
    url: str,
    impersonate: str | None = None,
    timeout: int = 15,
    headers: dict | None = None,
    max_retries: int | None = None,
):
    """Fetch URL with per-host semaphore, impersonation and simple retries.

    Returns the `curl_cffi.requests.Response` or raises the last exception.
    """
    sem = _get_host_semaphore(url)
    last_exc = None
    impersonations = (
        [impersonate] + [item for item in RETRY_IMPERSONATE if item != impersonate]
        if impersonate
        else RETRY_IMPERSONATE
    )
    retry_count = max_retries if max_retries is not None else len(impersonations)

    for attempt in range(retry_count):
        sem.acquire()
        try:
            try:
                chosen_imp = impersonations[attempt % len(impersonations)]
                # debug trace for impersonation attempts
                # Note: keep lightweight to avoid noisy logs
                logger.debug(
                    "fetch attempt=%d impersonate=%s url=%s", attempt, chosen_imp, url
                )
                resp = req.get(
                    url,
                    impersonate=cast(Any, chosen_imp),
                    timeout=timeout,
                    headers=headers,
                )
                # treat 200 as success
                if getattr(resp, "status_code", None) == 200:
                    return resp

                # On 403/429, try a lightweight fallback: request once without
                # impersonation and with simple headers (may bypass anti-bot).
                if getattr(resp, "status_code", None) in (403, 429):
                    status = getattr(resp, "status_code", None)
                    logger.debug(
                        "fetch received %s for %s (impersonate=%s)",
                        status,
                        url,
                        chosen_imp,
                    )
                    try:
                        alt_headers = dict(headers or {})
                        alt_headers.setdefault("User-Agent", HEADERS.get("User-Agent"))
                        alt_headers.setdefault(
                            "Accept-Language", HEADERS.get("Accept-Language")
                        )
                        logger.debug(
                            "fetch fallback: trying without impersonate for %s", url
                        )
                        alt_resp = req.get(
                            url, impersonate=None, timeout=timeout, headers=alt_headers
                        )
                        alt_status = getattr(alt_resp, "status_code", None)
                        if alt_status == 200:
                            return alt_resp
                        if alt_status not in (403, 429) and alt_status is not None:
                            return alt_resp
                        last_exc = Exception(f"HTTP {alt_status}")
                    except Exception as e:
                        last_exc = e

                    # longer backoff for anti-bot cases before retrying
                    time.sleep(2 + attempt * 2)
                    continue

                return resp
            except Exception as e:
                last_exc = e
                time.sleep(0.5 * (attempt + 1))
                continue
        finally:
            try:
                sem.release()
            except Exception:
                pass

    if last_exc:
        raise last_exc
    raise RuntimeError("fetch failed")


def strip_preview_emoji(s: str) -> str:
    """Remove common emoji and preview markers from a string for PDF/HTML output.

    This is intentionally conservative — it removes obvious emoji codepoints and
    falls back to stripping non-ASCII characters if the regex engine can't handle
    high Unicode ranges on this Python build.
    """
    if not isinstance(s, str):
        return s
    try:
        # Also strip regional indicator symbols (U+1F1E6-U+1F1FF) which form
        # flag emoji like 🇺🇸 — ReportLab fonts cannot render these and they
        # appear as small square boxes in PDFs. Keep the regex conservative.
        return re.sub(
            r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F1E6-\U0001F1FF\u2600-\u27BF\U0001F900-\U0001F9FF]",
            "",
            s,
        )
    except re.error:
        # Fallback for narrow builds — strip non-ASCII
        return re.sub(r"[^\x00-\x7F]", "", s)


def has_css_class(element, cls: str) -> bool:
    """Return True if BeautifulSoup element has the given CSS class."""
    try:
        classes = element.get("class", [])
        if isinstance(classes, str):
            classes = classes.split()
        return cls in (classes or [])
    except Exception:
        return False


# PNG preview helpers removed — PNG export disabled per user request


def clean_num(s):
    if not s:
        return None
    s = s.strip().replace(",", "").replace("\u2033", "").replace("\u2757", "")
    return s


def _has_meaningful_change(value):
    if value is None:
        return False
    text = str(value).strip()
    if text in ("", "None", "0", "0%"):
        return False
    try:
        numeric = text.replace("%", "").replace("+", "").replace(",", "")
        return float(numeric) != 0
    except ValueError:
        return True


def _has_meaningful_value(value):
    if value is None:
        return False
    return str(value).strip() not in ("", "None")


def is_valid_data(d):
    """Return True if a parsed data item looks usable."""
    if not isinstance(d, dict):
        return False
    close = d.get("close")
    if _has_meaningful_value(close):
        return True
    contracts = d.get("contracts")
    if isinstance(contracts, list):
        for contract in contracts:
            if not isinstance(contract, dict):
                continue
            if _has_meaningful_value(contract.get("price")):
                return True
            if _has_meaningful_change(contract.get("change")):
                return True
            if _has_meaningful_change(contract.get("change_pct")):
                return True
    return _has_meaningful_change(d.get("change")) or _has_meaningful_change(
        d.get("change_pct")
    )


def _cache_item_timestamp(cache_raw, cached_item):
    fetched_at = cached_item.get("fetched_at") if isinstance(cached_item, dict) else None
    if not fetched_at and isinstance(cache_raw, dict):
        fetched_at = cache_raw.get("timestamp")
    if not fetched_at:
        return None
    try:
        return datetime.fromisoformat(str(fetched_at))
    except Exception:
        return None


def is_recent_cached_item(
    cache_raw,
    cached_item,
    now_ts=None,
    max_age_seconds=CACHE_MAX_AGE_SECONDS,
):
    """Return True when cached data is valid and has a fresh parseable timestamp."""
    if not isinstance(cached_item, dict) or not is_valid_data(cached_item):
        return False

    then = _cache_item_timestamp(cache_raw, cached_item)
    if then is None:
        return False

    if then.tzinfo:
        now = datetime.now(then.tzinfo)
    elif now_ts is not None and getattr(now_ts, "tzinfo", None) is None:
        now = now_ts
    else:
        now = datetime.now()

    try:
        age_seconds = (now - then).total_seconds()
    except Exception:
        return False
    return -CACHE_MAX_CLOCK_SKEW_SECONDS <= age_seconds <= max_age_seconds


def normalize_cached_item_timestamp(cache_raw, cached_item):
    """Copy a cached item and preserve the timestamp that made it fresh."""
    if not isinstance(cached_item, dict):
        return cached_item
    normalized = dict(cached_item)
    if not normalized.get("fetched_at") and isinstance(cache_raw, dict):
        fetched_at = cache_raw.get("timestamp")
        if fetched_at:
            normalized["fetched_at"] = fetched_at
    return normalized
