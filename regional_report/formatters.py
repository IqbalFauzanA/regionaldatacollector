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


def _change_parts(d):
    point = get_point_change(d)
    pct = get_change(d)
    return [part for part in (point, pct) if part]


def fmt(
    d,
    *,
    prefix="",
    suffix="",
    suffix_numeric_only=False,
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

    return " ".join([f"{prefix}{cp}", *_change_parts(d)])


def decorate_value(d, base=None):
    """Bold large movers using the normalized percent change."""
    if base is None:
        base = fmt(d) if isinstance(d, dict) else str(d)

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

    def kv(label, *, decorate=True, **fmt_options):
        d = data.get(label)
        if d is None:
            return None
        if isinstance(d, dict) and not is_valid_data(d):
            return None
        value = fmt(d, **fmt_options)
        if not value:
            return None
        return decorate_value(d, value) if decorate else value

    def add_line(label, **options):
        value = kv(label, **options)
        if value:
            lines.append(f"- **{label}:** {value}")

    def add_group(labels, **options):
        for label in labels:
            add_line(label, **options)

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
            "Dow",
            "S&P 500",
            "Nasdaq",
            "S&P 500 VIX",
        ]
    )
    lines.append("")

    lines.append("## \U0001f1ea\U0001f1fa Europe")
    add_group(["FTSE", "DAX", "CAC"])
    lines.append("")

    lines.append("## \U0001f30f Asia")
    add_group(
        [
            "Nikkei",
            "Shanghai",
            "HSI",
            "KOSPI",
        ]
    )
    lines.append("")

    lines.append("## \U0001f1ee\U0001f1e9 Indonesia")
    add_line("IDX")
    add_group(
        [
            "LQ45",
            "Kompas 100",
            "IDX30",
        ]
    )
    add_group(
        [
            "IDX Energy",
            "IDX Basic Materials",
            "IDX Industrial",
            "IDX Technology",
            "IDX Finance",
            "IDX Banking",
            "IDX Infrastructure",
            "IDX Property",
            "IDX Transportation",
            "IDX Consumer Cyclical",
            "IDX Consumer Non-Cyclical",
            "IDX Healthcare",
        ]
    )
    lines.append("")

    add_line("USD/IDR")
    add_line("Jisdor")
    add_line("Indo10Yr", suffix="%", suffix_numeric_only=True)
    add_line("ICBI")
    add_line("IndoCDS 5yr")
    lines.append("")

    lines.append("## \U0001f4b5 FX & Bonds")
    add_line("EUR/USD")

    add_line("DXY", decorate=False)

    us_bonds = ["US2Yr", "US10Yr", "US30Yr"]
    if any(
        isinstance(data.get(label), dict) and is_valid_data(data.get(label))
        for label in us_bonds
    ):
        lines.append("- **US Treasuries:**")
        for label in us_bonds:
            d = data.get(label)
            if isinstance(d, dict) and is_valid_data(d):
                value = decorate_value(
                    d, fmt(d, suffix="%", suffix_numeric_only=True)
                )
                if value:
                    lines.append(f"  - **{label}:** {value}")
    lines.append("")

    lines.append("## \U0001f6e2\ufe0f Energy")
    add_group(["Oil WTI", "Oil Brent", "Nat Gas"], prefix="$")
    lines.append("")

    lines.append("### Coal (Barchart) \U0001f504")
    for label in ["Newcastle", "Rotterdam"]:
        coal = data.get(label)
        if isinstance(coal, dict) and coal.get("contracts"):
            lines.append(f"- **{label}:**")
            for contract in coal["contracts"]:
                d = {
                    "close": contract.get("price"),
                    "change": contract.get("change"),
                    "change_pct": contract.get("change_pct"),
                }
                value = decorate_value(d, fmt(d))
                month = contract.get("month", "")
                if month and value:
                    lines.append(f"  - **{month}:** {value}")
    lines.append("")

    lines.append("## \U0001f3d7\ufe0f Metals & Mining")
    add_line("Gold")
    gold_spot = data.get("Gold (XAU/USD)")
    if isinstance(gold_spot, dict) and is_valid_data(gold_spot):
        add_line("Gold (XAU/USD)")
    add_group(
        [
            "Silver",
            "Copper",
            "Nickel",
            "Timah",
            "Aluminium",
            "Iron Ore 62%",
            "BCOMIN",
        ]
    )
    lines.append("")

    lines.append("## \U0001f33f Komoditas Lain")
    add_group(["CPO", "Woodpulp", "Ammonia", "Corn", "Wheat", "Soybean Oil"])
    lines.append("")

    lines.append("## \U0001f4c8 ETFs & Stocks")
    for label in ["EIDO", "TLKM", "EEM"]:
        add_line(label)
        if label == "TLKM":
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
