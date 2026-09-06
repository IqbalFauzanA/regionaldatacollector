"""Constants and instrument mapping dictionaries."""

MAJOR_INDEX_KEYS = (
    "Dow",
    "S&P 500",
    "Nasdaq",
    "S&P 500 VIX",
    "DAX",
    "FTSE",
    "CAC",
    "Nikkei 225",
    "Shanghai",
    "HSI",
    "KOSPI",
)

CNBC_US_INDEX_PAGES = (
    ("Dow", "https://www.cnbc.com/quotes/.DJI"),
    ("Nasdaq", "https://www.cnbc.com/quotes/.IXIC"),
    ("S&P 500", "https://www.cnbc.com/quotes/.SPX"),
)

CNBC_US_INDEX_KEYS = tuple(report_label for report_label, _ in CNBC_US_INDEX_PAGES)

INVESTING_MAJOR_INDEX_KEYS = tuple(
    key for key in MAJOR_INDEX_KEYS if key not in CNBC_US_INDEX_KEYS
)

IDX_INDEX_KEYS = ("IDX", "LQ45", "IDX30", "Kompas 100")

IDX_SECTOR_KEYS = (
    "IDX Energy",
    "IDX Basic Materials",
    "IDX Industrial",
    "IDX Consumer Non-Cyclical",
    "IDX Healthcare",
    "IDX Consumer Cyclical",
    "IDX Technology",
    "IDX Transportation",
    "IDX Infrastructure",
    "IDX Finance",
    "IDX Banking",
)

COMMODITY_FUTURES_KEYS = (
    "Oil WTI",
    "Oil Brent",
    "Nat Gas",
    "Aluminium",
    "Nickel",
)

US_BOND_KEYS = ("US2Yr", "US10Yr", "US30Yr")

REQUESTED_SOURCE_BY_KEY = {
    "Dow": "CNBC",
    "Nasdaq": "CNBC",
    "S&P 500": "CNBC",
    "USD/IDR": "Bloomberg",
    "Gold": "Bloomberg",
    "Gold (XAU/USD)": "Bloomberg",
    "Silver": "Bloomberg",
    "Copper": "Bloomberg",
    "DXY": "Bloomberg",
    "EUR/USD": "Bloomberg",
    "Timah": "Bloomberg",
    "Corn": "Bloomberg",
    "Wheat": "Bloomberg",
    "Soybean Oil": "Bloomberg",
    "Ammonia": "SunSirs",
    "IndoCDS 5yr": "WorldGovernmentBonds",
    "CPO": "Bursa Malaysia",
    "KOSPI": "KOSPI 50",
    "Oil WTI": "Bloomberg",
    "Oil Brent": "Bloomberg",
}
