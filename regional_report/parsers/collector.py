"""Concurrent data collection orchestration."""

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from regional_report.commons import (
    CACHE_MAX_AGE_SECONDS,
    MAX_FETCH_WORKERS,
    is_recent_cached_item,
    normalize_cached_item_timestamp,
)
from .barchart import parse_barchart_coal
from .base import parse_table_pages
from .bloomberg import (
    parse_bloomberg_agriculture,
    parse_bloomberg_brent,
    parse_bloomberg_dxy,
    parse_bloomberg_eurusd,
    parse_bloomberg_metals,
    parse_bloomberg_tin,
    parse_bloomberg_usdidr,
    parse_bloomberg_wti,
)
from .bursa import parse_bursa_cpo
from .cnbc import parse_cnbc_quote_pages
from .constants import (
    CNBC_US_INDEX_KEYS,
    CNBC_US_INDEX_PAGES,
    COMMODITY_FUTURES_KEYS,
    IDX_INDEX_KEYS,
    IDX_SECTOR_KEYS,
    INVESTING_MAJOR_INDEX_KEYS,
    REQUESTED_SOURCE_BY_KEY,
    US_BOND_KEYS,
)
from .indonesia import (
    parse_indonesia_bonds,
    parse_indonesia_cds,
    parse_jisdor,
    parse_phei,
)
from .investing import parse_commodities_futures, parse_instrument_page
from .sunsirs import parse_ammonia, parse_sunsirs_woodpulp
from .yahoo import (
    parse_yahoo_finance,
    parse_yahoo_idx_property,
    parse_yahoo_sector_indices,
)


def collect_data(
    cache_raw=None,
    cache_max_age_seconds=CACHE_MAX_AGE_SECONDS,
    comparison_cache_raw=None,
):
    """Run all scrapers, return (data_dict, sources_list, timestamp)."""
    DATA = {}

    def log(msg):
        print(msg, file=sys.stderr, flush=True)

    cached_data = (
        cache_raw.get("data", {})
        if isinstance(cache_raw, dict) and isinstance(cache_raw.get("data"), dict)
        else {}
    )
    comparison_cache_raw = comparison_cache_raw or cache_raw
    comparison_cached_data = (
        comparison_cache_raw.get("data", {})
        if isinstance(comparison_cache_raw, dict)
        and isinstance(comparison_cache_raw.get("data"), dict)
        else {}
    )
    cache_now = datetime.now()

    def fresh_cached_results(keys):
        results = {}
        if not cached_data or not keys:
            return results
        for key in keys:
            cached_item = cached_data.get(key)
            required_source = REQUESTED_SOURCE_BY_KEY.get(key)
            if required_source and (
                not isinstance(cached_item, dict)
                or cached_item.get("source") != required_source
            ):
                continue
            if is_recent_cached_item(
                cache_raw, cached_item, cache_now, cache_max_age_seconds
            ):
                results[key] = normalize_cached_item_timestamp(cache_raw, cached_item)
        return results

    def run_task(label, fn):
        try:
            return fn()
        except Exception as e:
            log(f"  WARN {label}: {type(e).__name__}: {str(e)[:80]}")
            return {}

    log("Regional Screener -- collecting data...")
    t0 = datetime.now()

    single_pages = [
        ("KOSPI 50", "https://www.investing.com/indices/kospi-50", "KOSPI"),
        (
            "Iron Ore",
            "https://www.investing.com/commodities/iron-ore-62-cfr-futures",
            "Iron Ore 62%",
        ),
        (
            "BCOMIN",
            "https://www.investing.com/indices/bloomberg-industrial-metals",
            "BCOMIN",
        ),
    ]
    tasks = [
        ("Coal from Barchart", parse_barchart_coal, ("Newcastle", "Rotterdam")),
        ("IDX Sector Indices", parse_yahoo_sector_indices, IDX_SECTOR_KEYS),
        ("JISDOR", parse_jisdor, ("Jisdor",)),
        (
            "CNBC US Indices",
            lambda: parse_cnbc_quote_pages(CNBC_US_INDEX_PAGES),
            CNBC_US_INDEX_KEYS,
        ),
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
                ],
                INVESTING_MAJOR_INDEX_KEYS,
            ),
            INVESTING_MAJOR_INDEX_KEYS,
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
                ],
                IDX_INDEX_KEYS,
            ),
            IDX_INDEX_KEYS,
        ),
        ("SunSirs Woodpulp", parse_sunsirs_woodpulp, ("Woodpulp",)),
        ("Commodities", parse_commodities_futures, COMMODITY_FUTURES_KEYS),
        ("Bloomberg USD/IDR", parse_bloomberg_usdidr, ("USD/IDR",)),
        ("Bloomberg DXY", parse_bloomberg_dxy, ("DXY",)),
        ("Bloomberg EUR/USD", parse_bloomberg_eurusd, ("EUR/USD",)),
        ("Bloomberg Tin", parse_bloomberg_tin, ("Timah",)),
        ("Bloomberg WTI", parse_bloomberg_wti, ("Oil WTI",)),
        ("Bloomberg Brent", parse_bloomberg_brent, ("Oil Brent",)),
        (
            "Bloomberg Metals",
            parse_bloomberg_metals,
            ("Gold", "Gold (XAU/USD)", "Silver", "Copper"),
        ),
        (
            "Bloomberg Agriculture",
            parse_bloomberg_agriculture,
            ("Corn", "Wheat", "Soybean Oil"),
        ),
        *[
            (
                label,
                lambda url=url, label=label, report_label=report_label: (
                    parse_instrument_page(url, label, report_label)
                ),
                (report_label,),
            )
            for label, url, report_label in single_pages
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
                ],
                US_BOND_KEYS,
            ),
            US_BOND_KEYS,
        ),
        ("Indo Bonds", parse_indonesia_bonds, ("Indo10Yr",)),
        ("PHEI (ICBI + Indo10Yr)", parse_phei, ("ICBI", "Indo10Yr")),
        *[
            (
                report_label,
                lambda ticker=ticker, report_label=report_label: parse_yahoo_finance(
                    ticker, report_label
                ),
                (report_label,),
            )
            for ticker, report_label in [
                ("EIDO", "EIDO"),
                ("EEM", "EEM"),
                ("TLK", "TLKM"),
            ]
        ],
        (
            "IndoCDS",
            lambda: parse_indonesia_cds(comparison_cached_data.get("IndoCDS 5yr")),
            ("IndoCDS 5yr",),
        ),
        ("SunSirs Ammonia", parse_ammonia, ("Ammonia",)),
        ("IDX Property", parse_yahoo_idx_property, ("IDX Property",)),
        ("Bursa CPO", parse_bursa_cpo, ("CPO",)),
    ]

    results_by_index = {}
    tasks_to_run = []
    cached_task_count = 0
    for idx, (label, fn, expected_keys) in enumerate(tasks):
        cached_results = fresh_cached_results(expected_keys)
        if expected_keys and len(cached_results) == len(expected_keys):
            results_by_index[idx] = cached_results
            cached_task_count += 1
            log(f"  cached: {label}")
            continue
        tasks_to_run.append((idx, label, fn))

    if tasks_to_run:
        log(
            f"Submitting {len(tasks_to_run)} scraper tasks with "
            f"{MAX_FETCH_WORKERS} workers..."
        )
        if cached_task_count:
            log(f"Using cache for {cached_task_count} scraper tasks.")
        with ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS) as executor:
            futures = {
                executor.submit(run_task, label, fn): idx
                for idx, label, fn in tasks_to_run
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
    else:
        log(f"All {len(tasks)} scraper tasks satisfied from cache.")

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
