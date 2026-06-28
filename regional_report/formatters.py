"""Markdown and WhatsApp report formatters."""

import re
from datetime import datetime

from .commons import is_valid_data


ALERT_MARKER = "\u203c\ufe0f"
BULLET = "\u2022"
EM_DASH = "\u2014"


def close_str(d):
    if isinstance(d, dict):
        return d.get("close", "")
    if isinstance(d, str):
        return d
    return ""


def get_change(d):
    if not isinstance(d, dict):
        return ""
    pct = d.get("change_pct", "")
    if pct is not None and str(pct).strip() not in ("", "None"):
        try:
            pct_num = abs(
                float(str(pct).replace("%", "").replace("+", "").replace(",", ""))
            )
            if pct_num > 50:
                return ""
        except ValueError:
            pass

        s = str(pct).strip()
        if not s.endswith("%"):
            s += "%"
        if not s.startswith(("+", "-")):
            s = "+" + s
        return s
    return ""


def get_point_change(d):
    if not isinstance(d, dict):
        return ""
    chg = d.get("change", "")
    if chg is not None and str(chg).strip() not in ("", "None"):
        s = str(chg).strip()
        if not s.startswith(("+", "-")):
            s = "+" + s
        return s
    return ""


def _change_parts(d, suppress_bad_point=False):
    point = get_point_change(d)
    if suppress_bad_point and isinstance(point, str) and point.startswith("-19"):
        point = ""
    pct = get_change(d)
    return [part for part in (point, pct) if part]


def fmt(
    d,
    *,
    prefix="",
    suffix="",
    suffix_numeric_only=False,
    suppress_bad_point=False,
):
    """Format a data item as close, point change, and percent change."""
    if isinstance(d, str):
        return d
    if d is None:
        return ""
    if not isinstance(d, dict):
        return str(d)

    close = close_str(d)
    if not close:
        return ""

    cp = str(close).strip()
    if suffix and not cp.endswith(str(suffix)):
        if suffix_numeric_only:
            try:
                float(cp)
                cp = f"{cp}{suffix}"
            except (TypeError, ValueError):
                pass
        else:
            cp = f"{cp}{suffix}"

    return " ".join([f"{prefix}{cp}", *_change_parts(d, suppress_bad_point)])


def decorate_value(d, base=None):
    """Bold large movers using the normalized percent change."""
    if base is None:
        base = fmt(d, suppress_bad_point=True) if isinstance(d, dict) else str(d)

    base = str(base).strip()
    if not base:
        return base

    pct = get_change(d) if isinstance(d, dict) else ""
    if not pct or pct in ("", "0", "0%"):
        return base

    try:
        abs_pct = abs(float(str(pct).replace("%", "").replace("+", "").replace(",", "")))
    except ValueError:
        return base

    if abs_pct > 3.0:
        return f"**{base}** {ALERT_MARKER}"
    if abs_pct > 2.0:
        return f"**{base}**"
    return base


def _tlkm_idr_equivalent(data):
    """Convert the TLKM ADR price to its approximate local-share IDR value."""
    try:
        tlkm = float(str(close_str(data.get("TLKM"))).replace(",", ""))
        jisdor = float(str(close_str(data.get("Jisdor"))).replace(",", ""))
    except (AttributeError, TypeError, ValueError):
        return None
    if tlkm <= 0 or jisdor <= 0:
        return None

    # One TLKM ADR represents 100 local shares.
    converted = tlkm * jisdor / 100
    return int(converted + 0.5)


def format_report(data, market_news=None):
    """Build the full report text."""
    lines = []

    def kv(key, *, suppress_bad_point=True, decorate=True, **fmt_options):
        d = data.get(key)
        if d is None:
            return None
        if isinstance(d, dict) and not is_valid_data(d):
            return None
        value = fmt(d, suppress_bad_point=suppress_bad_point, **fmt_options)
        if not value:
            return None
        return decorate_value(d, value) if decorate else value

    def add_line(label, key, *, label_prefix="", **options):
        value = kv(key, **options)
        if value:
            lines.append(f"- **{label_prefix}{label}:** {value}")

    def add_group(items, *, label_prefix="", **options):
        for key, label in items:
            add_line(label, key, label_prefix=label_prefix, **options)

    now = datetime.now()
    hari = [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ][now.weekday()]
    bulan = [
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    ][now.month - 1]

    lines.append("# \U0001f4ca Good Morning")
    lines.append(f"_\U0001f5d3\ufe0f {hari}, {now.day} {bulan} {now.year}_")
    lines.append("")
    lines.append("---")
    lines.append("")

    news = [
        item
        for item in (market_news or [])
        if isinstance(item, dict) and item.get("title") and item.get("url")
    ]
    if news:
        lines.append("## \U0001f4f0 Market News Summary")
        lines.append("")
        lines.append("### Top Market News")
        for item in news:
            lines.append(f"- [{item['title']}]({item['url']})")
        lines.append("")

    lines.append("---")
    lines.append("")

    lines.append("## \U0001f1fa\U0001f1f8 US Indices")
    add_group(
        [
            ("Dow", "Dow"),
            ("S&P 500", "S&P 500"),
            ("Nasdaq", "Nasdaq"),
            ("S&P 500 VIX", "S&P 500 VIX"),
        ]
    )
    lines.append("")

    lines.append("## \U0001f1ea\U0001f1fa Europe")
    add_group([("DAX", "DAX"), ("FTSE", "FTSE"), ("CAC", "CAC")])
    lines.append("")

    lines.append("## \U0001f30f Asia")
    add_group(
        [
            ("Nikkei", "Nikkei"),
            ("Shanghai", "Shanghai"),
            ("HSI", "HSI"),
            ("KOSPI", "KOSPI"),
            ("STI", "STI"),
        ]
    )
    lines.append("")

    lines.append("## \U0001f1ee\U0001f1e9 Indonesia")
    add_line("IDX", "IDX")
    add_group(
        [
            ("LQ45", "LQ45"),
            ("IDX Kompas 100", "Kompas 100"),
            ("IDX30", "IDX30"),
        ]
    )
    add_group(
        [
            ("IDXEnergy", "Energy"),
            ("IDX BscMat", "Basic Materials"),
            ("IDXIndst", "Industrial"),
            ("IDX Tech", "Technology"),
            ("IDX Finance", "Finance"),
            ("IDX Banking", "Banking"),
            ("IDX Infra", "Infrastructure"),
            ("IDX Property", "Property"),
            ("IDX Transprt", "Transportation"),
            ("IDXCYCLC", "Consumer Cyclical"),
            ("IDXNONCYC", "Consumer Non-Cyclical"),
            ("IDXHlthcare", "Healthcare"),
        ],
        label_prefix="IDX ",
    )
    lines.append("")

    add_line("USD/IDR", "IDR", suppress_bad_point=False)
    add_line("Jisdor", "Jisdor")
    add_line("Indo10Yr", "Indo10Yr", suffix="%", suffix_numeric_only=True)
    add_line("ICBI", "ICBI", suppress_bad_point=False)
    add_line("IndoCDS 5yr", "IndoCDS 5yr", suppress_bad_point=False)
    lines.append("")

    lines.append("## \U0001f4b5 FX & Bonds")
    add_line("EUR/USD", "Euro", suppress_bad_point=False)

    dxy = data.get("USDIndx")
    if isinstance(dxy, dict) and is_valid_data(dxy):
        dxy_fmt = fmt(dxy, suppress_bad_point=False)
        if dxy_fmt:
            lines.append(f"- **DXY:** {dxy_fmt}")

    us_bonds = [
        ("US2Yr", "US2Yr"),
        ("US10Yr", "US10Yr"),
        ("US30Yr", "US30Yr"),
    ]
    if any(
        isinstance(data.get(key), dict) and is_valid_data(data.get(key))
        for key, _ in us_bonds
    ):
        lines.append("- **US Treasuries:**")
        for key, label in us_bonds:
            d = data.get(key)
            if isinstance(d, dict) and is_valid_data(d):
                value = decorate_value(
                    d, fmt(d, suffix="%", suffix_numeric_only=True)
                )
                if value:
                    lines.append(f"  - **{label}:** {value}")
    lines.append("")

    lines.append("## \U0001f6e2\ufe0f Energy")
    for key, label in [
        ("Oil(WT)", "Oil WTI"),
        ("Oil(Brn)", "Oil Brent"),
        ("Ntrl Gas", "Nat Gas"),
    ]:
        add_line(label, key, prefix="$")
    lines.append("")

    lines.append("### Coal (Barchart) \U0001f504")
    for key, label in [("Coal(Nwl)", "Newcastle"), ("Coal(Rot)", "Rotterdam")]:
        coal = data.get(key)
        if isinstance(coal, dict) and coal.get("contracts"):
            lines.append(f"- **{label}:**")
            for contract in coal["contracts"]:
                d = {
                    "close": contract.get("price"),
                    "change": contract.get("change"),
                    "change_pct": contract.get("change_pct"),
                }
                value = decorate_value(d, fmt(d, suppress_bad_point=True))
                month = contract.get("month", "")
                if month and value:
                    lines.append(f"  - **{month}:** {value}")
    lines.append("")

    lines.append("## \U0001f3d7\ufe0f Metals & Mining")
    add_line("Gold", "Gold")
    gold_spot = data.get("Gold(Spot)")
    if isinstance(gold_spot, dict) and is_valid_data(gold_spot):
        add_line("Gold", "Gold(Spot)")
        lines.append("     (XAU/USD)")
    add_group(
        [
            ("Silver", "Silver"),
            ("Copper", "Copper"),
            ("Nickel", "Nickel"),
            ("Timah", "Timah"),
            ("Aluminium", "Aluminium"),
            ("Iron Ore 62%", "Iron Ore 62%"),
            ("BCOMIN", "BCOMIN"),
        ]
    )
    lines.append("")

    lines.append("## \U0001f33f Komoditas Lain")
    for key, label in [
        ("CPO", "CPO"),
        ("Woodpulp", "Woodpulp"),
        ("Ammonia", "Ammonia"),
        ("Corn", "Corn"),
        ("Wheat", "Wheat"),
        ("SoybeanOil", "Soybean Oil"),
    ]:
        value = kv(key)
        if not value:
            continue
        lines.append(f"- **{label}:** {value}")
    lines.append("")

    lines.append("## \U0001f4c8 ETFs & Stocks")
    for key, label in [("EIDO", "EIDO"), ("TLKM", "TLKM"), ("EEM", "EEM")]:
        add_line(label, key, suppress_bad_point=False)
        if key == "TLKM":
            tlkm_idr = _tlkm_idr_equivalent(data)
            if tlkm_idr is not None:
                lines.append(f"        ({tlkm_idr})")

    lines.append("---")
    lines.append("## Footer")
    lines.append("- **Broker Code:** AT")
    lines.append("- **Prepared by:** Desy Erawati / DE")
    lines.append(
        "- **Sources:** Bloomberg, Investing, IBPA, CNBC, Bursa Malaysia, SunSirs"
    )
    lines.append("- **Copyright:** Phintraco Sekuritas")

    return "\n".join(lines)


def format_report_whatsapp(report_md):
    """Convert the generated Markdown report into WhatsApp-friendly text."""
    out_lines = []
    in_top_market_news = False

    for raw in report_md.splitlines():
        line = raw.rstrip()
        if not line:
            out_lines.append("")
            continue

        if line.strip() == "---":
            out_lines.append("")
            continue

        heading = re.match(r"^#{1,6}\s*(.+)$", line)
        if heading:
            content = heading.group(1).strip()
            content = re.sub(r"\*\*(.+?)\*\*", r"*\1*", content)
            in_top_market_news = "top market news" in content.lower()
            content = re.sub(
                r"\[([^\]]+)\]\(([^)]+)\)",
                rf"\1 {EM_DASH} \2",
                content,
            )
            out_lines.append(f"*{content}*")
            continue

        bullet = re.match(r"^(\s*)-\s+(.*)$", line)
        if bullet:
            indent = bullet.group(1)
            content = bullet.group(2)
            content = re.sub(r"\*\*(.+?)\*\*", r"*\1*", content)
            if in_top_market_news:
                content = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", content)
            else:
                content = re.sub(
                    r"\[([^\]]+)\]\(([^)]+)\)",
                    rf"\1 {EM_DASH} \2",
                    content,
                )
            out_lines.append(f"{indent}{BULLET} {content}")
            continue

        line = re.sub(r"\*\*(.+?)\*\*", r"*\1*", line)
        if in_top_market_news:
            line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", line)
        else:
            line = re.sub(
                r"\[([^\]]+)\]\(([^)]+)\)",
                rf"\1 {EM_DASH} \2",
                line,
            )
        out_lines.append(line)

    return "\n".join(out_lines)
