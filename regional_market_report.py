#!/usr/bin/env python3
"""
Regional Market Report CLI entrypoint.

The implementation lives in the regional_report package so shared fetch/config,
parsers, formatters, and exports can evolve independently.
"""

import json
import logging
import os
import sys

from regional_report.commons import CACHE_DIR, CACHE_JSON, OUTPUT_DIR, REPORT_WA
from regional_report.commons import (
    is_recent_cached_item,
    is_valid_data,
    normalize_cached_item_timestamp,
)
from regional_report.exports import save_report_exports
from regional_report.formatters import format_report, format_report_whatsapp
from regional_report.parsers import (
    REQUESTED_SOURCE_BY_KEY,
    collect_data,
    fetch_market_news,
)

logger = logging.getLogger(__name__)


def _configure_logging(argv):
    debug_mode = "--debug" in argv or "--verbose" in argv
    if debug_mode:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")
        logger.debug("Logging enabled: DEBUG")
    else:
        logging.basicConfig(level=logging.WARNING)


def _load_cache():
    if not os.path.exists(CACHE_JSON):
        return None
    try:
        with open(CACHE_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _merge_partial_cache(cache_raw, data, sources, timestamp):
    raw_out = {
        "timestamp": timestamp,
        "data": data,
        "sources_used": sources,
    }
    if not cache_raw or not isinstance(cache_raw.get("data"), dict):
        return raw_out

    cached_data = cache_raw.get("data", {})
    merged = {}
    used_cached = False
    ordered_keys = list(data) + [key for key in cached_data if key not in data]
    for key in ordered_keys:
        value = data.get(key)
        if is_valid_data(value):
            merged[key] = value
            continue

        cached_item = cached_data.get(key)
        required_source = REQUESTED_SOURCE_BY_KEY.get(key)
        if required_source and (
            not isinstance(cached_item, dict)
            or cached_item.get("source") != required_source
        ):
            continue
        if is_recent_cached_item(cache_raw, cached_item):
            merged[key] = normalize_cached_item_timestamp(cache_raw, cached_item)
            used_cached = True
        elif key in data:
            merged[key] = value

    raw_out["data"] = merged
    cached_sources = cache_raw.get("sources_used", []) if used_cached else []
    raw_out["sources_used"] = sorted(set(sources) | set(cached_sources))
    return raw_out


def _write_cache(raw_out):
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        with open(CACHE_JSON, "w", encoding="utf-8") as f:
            json.dump(raw_out, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(
            f"[WARN] failed to write cache: {type(e).__name__}: {str(e)[:80]}",
            file=sys.stderr,
        )


def _save_outputs(report):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    try:
        wa_text = format_report_whatsapp(report)
        with open(REPORT_WA, "w", encoding="utf-8") as f:
            f.write(wa_text)
        print(f"[Report saved to {REPORT_WA}]", file=sys.stderr, flush=True)
    except Exception as e:
        print(
            f"[WhatsApp export skipped: {type(e).__name__}: {str(e)[:80]}]",
            file=sys.stderr,
            flush=True,
        )

    for export_path in save_report_exports(report):
        print(f"[Report saved to {export_path}]", file=sys.stderr, flush=True)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    json_only = "--json-only" in argv
    from_cache = "--from-cache" in argv
    partial_cache_mode = "--partial-cache" in argv

    if getattr(sys, "frozen", False) and "--partial-cache" not in argv:
        partial_cache_mode = True

    _configure_logging(argv)
    cache_raw = _load_cache()

    if from_cache and cache_raw:
        print("[Loading from cached screener data...]", file=sys.stderr, flush=True)
        data = cache_raw.get("data", {})
        sources = cache_raw.get("sources_used", [])
        timestamp = cache_raw.get("timestamp", "")
    else:
        data, sources, timestamp = collect_data(
            cache_raw=cache_raw if partial_cache_mode else None
        )
        if partial_cache_mode:
            raw_out = _merge_partial_cache(cache_raw, data, sources, timestamp)
        else:
            raw_out = {
                "timestamp": timestamp,
                "data": data,
                "sources_used": sources,
            }
        _write_cache(raw_out)
        data = raw_out["data"]
        sources = raw_out["sources_used"]

    if json_only:
        print(
            json.dumps(
                {"timestamp": timestamp, "data": data, "sources_used": sources},
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    market_news = fetch_market_news(5)
    report = format_report(data, market_news=market_news)
    print(report, flush=True)
    _save_outputs(report)


if __name__ == "__main__":
    main()
