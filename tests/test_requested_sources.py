import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from regional_report.formatters import format_report_whatsapp, format_report
from regional_report.parsers import (
    _barchart_contract_months,
    parse_bloomberg_agriculture_html,
    parse_bloomberg_metals_html,
    parse_bloomberg_quote_html,
    parse_bloomberg_usdidr_html,
    parse_bursa_cpo_payload,
    parse_commodities_futures,
    parse_instrument_page,
    parse_ammonia,
    parse_indonesia_cds_payload,
    parse_jisdor,
    parse_sunsirs_woodpulp,
    parse_yahoo_sector_indices,
    REQUESTED_SOURCE_BY_KEY,
)


def next_data_html(page_props):
    payload = {"props": {"pageProps": page_props}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'


class RequestedSourceParserTests(unittest.TestCase):
    def test_barchart_coal_contract_months_roll_forward(self):
        self.assertEqual(
            _barchart_contract_months(datetime(2026, 7, 1)),
            [
                ("Jul", "N", 26),
                ("Aug", "Q", 26),
                ("Sep", "U", 26),
                ("Oct", "V", 26),
            ],
        )
        self.assertEqual(
            _barchart_contract_months(datetime(2026, 11, 1)),
            [
                ("Nov", "X", 26),
                ("Dec", "Z", 26),
                ("Jan", "F", 27),
                ("Feb", "G", 27),
            ],
        )

    def test_bloomberg_usdidr_quote(self):
        html = next_data_html(
            {
                "quote": {
                    "id": "USDIDR:CUR",
                    "price": "17,922.0000",
                    "priceChange1Day": -21,
                    "percentChange1Day": -0.117,
                    "issuedCurrency": "IDR",
                }
            }
        )
        item = parse_bloomberg_usdidr_html(html)["IDR"]
        self.assertEqual(item["close"], "17922.0000")
        self.assertEqual(item["change"], "-21.00")
        self.assertEqual(item["change_pct"], "-0.12%")
        self.assertEqual(item["source"], "Bloomberg")

    def test_bloomberg_requested_metals(self):
        securities = [
            {
                "id": ticker,
                "price": price,
                "priceChange1Day": change,
                "percentChange1Day": percent,
            }
            for ticker, price, change, percent in [
                ("GC1:COM", "4,043.50", 27.1, 0.67),
                ("XAUUSD:CUR", "4,024.1400", 19.51, 0.49),
                ("SI1:COM", "59.67", 0.876, 1.49),
                ("HG1:COM", "620.70", 6.95, 1.13),
            ]
        ]
        html = next_data_html(
            {
                "sectionFront": {
                    "sectionFrontTab": {"sections": [{"securities": securities}]}
                }
            }
        )
        result = parse_bloomberg_metals_html(html)
        self.assertEqual(set(result), {"Gold", "Gold(Spot)", "Silver", "Copper"})
        self.assertEqual(result["Silver"]["change"], "+0.88")
        self.assertEqual(result["Copper"]["close"], "620.70")

    def test_bloomberg_requested_agriculture(self):
        securities = [
            {
                "id": ticker,
                "price": price,
                "priceChange1Day": change,
                "percentChange1Day": percent,
            }
            for ticker, price, change, percent in [
                ("C 1:COM", "429.25", -2.75, -0.64),
                ("W 1:COM", "538.00", 4.25, 0.80),
                ("BO1:COM", "55.87", 0.31, 0.56),
            ]
        ]
        html = next_data_html(
            {
                "sectionFront": {
                    "sectionFrontTab": {"sections": [{"securities": securities}]}
                }
            }
        )
        result = parse_bloomberg_agriculture_html(html)
        self.assertEqual(set(result), {"Corn", "Wheat", "SoybeanOil"})
        self.assertEqual(result["Corn"]["change"], "-2.75")
        self.assertEqual(result["Wheat"]["change_pct"], "+0.80%")
        self.assertEqual(result["SoybeanOil"]["ticker"], "BO1:COM")
        for item in result.values():
            self.assertEqual(item["source"], "Bloomberg")
        for key in result:
            self.assertEqual(REQUESTED_SOURCE_BY_KEY[key], "Bloomberg")

    def test_bloomberg_dxy_eurusd_and_tin_quotes(self):
        cases = [
            ("DXY:CUR", "USDIndx", "101.3570", -0.07, -0.07),
            ("EURUSD:CUR", "Euro", "1.1384", 0.0014, 0.12),
            ("LMSNDS03:COM", "Timah", "50,553.00", 170, 0.34),
        ]
        for ticker, code, price, change, percent in cases:
            with self.subTest(ticker=ticker):
                html = next_data_html(
                    {
                        "quote": {
                            "id": ticker,
                            "price": price,
                            "priceChange1Day": change,
                            "percentChange1Day": percent,
                        }
                    }
                )
                item = parse_bloomberg_quote_html(html, ticker, code)[code]
                self.assertEqual(item["source"], "Bloomberg")
                self.assertEqual(item["ticker"], ticker)
        self.assertEqual(
            parse_bloomberg_quote_html(
                next_data_html(
                    {
                        "quote": {
                            "id": "LMSNDS03:COM",
                            "price": "50,553.00",
                            "priceChange1Day": 170,
                            "percentChange1Day": 0.34,
                        }
                    }
                ),
                "LMSNDS03:COM",
                "Timah",
            )["Timah"]["close"],
            "50553.00",
        )

    def test_bursa_third_day_row_last_done(self):
        payload = {
            "data": [
                [1, "FCPO", "Jul 2026", "", "", "", "4,507.0000", "-6.0000"],
                [2, "FCPO", "Aug 2026", "", "", "", "4,536.0000", "+1.0000"],
                [
                    3,
                    "<div><span></span>FCPO</div>",
                    "Sep 2026",
                    "",
                    "",
                    "",
                    "4,566.0000",
                    '<span class="text-success">+9.0000</span>',
                ],
            ]
        }
        item = parse_bursa_cpo_payload(payload)["CPO"]
        self.assertEqual(item["close"], "4566.00")
        self.assertEqual(item["change"], "+9.00")
        self.assertEqual(item["contract"], "Sep 2026")

    def test_commodity_fallback_runs_only_after_all_tables_are_searched(self):
        html = """
            <table>
              <tr><th>Name</th><th>Last</th><th>Change</th><th>Change %</th></tr>
              <tr><td>Crude Oil WTI</td><td>70.00</td><td>+1.00</td><td>+1.45%</td></tr>
              <tr><td>Brent Oil</td><td>75.00</td><td>+1.00</td><td>+1.35%</td></tr>
            </table>
            <table>
              <tr><th>Name</th><th>Last</th><th>Change</th><th>Change %</th></tr>
              <tr><td>Natural Gas</td><td>3.00</td><td>+0.10</td><td>+3.45%</td></tr>
              <tr><td>Aluminium</td><td>2500.00</td><td>+5.00</td><td>+0.20%</td></tr>
              <tr><td>Nickel</td><td>15000.00</td><td>-5.00</td><td>-0.03%</td></tr>
            </table>
        """
        with (
            patch(
                "regional_report.parsers.fetch",
                return_value=SimpleNamespace(text=html),
            ),
            patch("regional_report.parsers.parse_instrument_page") as fallback,
        ):
            result = parse_commodities_futures()

        self.assertEqual(
            set(result), {"Oil(WT)", "Oil(Brn)", "Ntrl Gas", "Aluminium", "Nickel"}
        )
        fallback.assert_not_called()

    def test_commodity_rows_are_not_overwritten_by_similarly_named_contracts(self):
        html = """
            <table>
              <tr>
                <th></th><th>Name</th><th>Month</th><th>Last</th>
                <th>High</th><th>Low</th><th>Chg.</th><th>Chg. %</th><th>Time</th>
              </tr>
              <tr><td></td><td>Crude Oil WTI derived</td><td>Aug 26</td><td>70.11</td><td>71.56</td><td>69.73</td><td>-0.64</td><td>-0.90%</td><td>12:15</td></tr>
              <tr><td></td><td>Brent Oil derived</td><td>Sep 26</td><td>73.68</td><td>74.83</td><td>73.08</td><td>-0.23</td><td>-0.31%</td><td>12:15</td></tr>
              <tr><td></td><td>Natural Gas derived</td><td>Aug 26</td><td>3.319</td><td>3.322</td><td>3.160</td><td>+0.138</td><td>+4.34%</td><td>12:15</td></tr>
              <tr><td></td><td>Dutch TTF Natural Gas</td><td>Aug 26</td><td>43.580</td><td>43.945</td><td>42.090</td><td>+0.964</td><td>+2.26%</td><td>12:15</td></tr>
              <tr><td></td><td>US Soybean Oil derived</td><td>Dec 26</td><td>20.88</td><td>21.00</td><td>20.00</td><td>-0.95</td><td>-4.33%</td><td>12:15</td></tr>
              <tr><td></td><td>Aluminium derived</td><td></td><td>3,091.40</td><td>3,141.50</td><td>3,084.90</td><td>-8.40</td><td>-0.27%</td><td>12:15</td></tr>
              <tr><td></td><td>Nickel derived</td><td></td><td>16,308.63</td><td>16,603.88</td><td>16,166.00</td><td>+2.00</td><td>+0.01%</td><td>12:15</td></tr>
            </table>
        """
        with (
            patch(
                "regional_report.parsers.fetch",
                return_value=SimpleNamespace(text=html),
            ),
            patch("regional_report.parsers.parse_instrument_page") as fallback,
        ):
            result = parse_commodities_futures()

        self.assertEqual(
            result["Oil(WT)"],
            {
                "close": "70.11",
                "change": "-0.64",
                "change_pct": "-0.90%",
                "source": "Investing Futures",
            },
        )
        self.assertEqual(result["Oil(Brn)"]["close"], "73.68")
        self.assertEqual(result["Ntrl Gas"]["close"], "3.319")
        fallback.assert_not_called()

    def test_kospi_50_is_authoritative_kospi_source(self):
        payload = {
            "props": {
                "pageProps": {
                    "state": {
                        "indexStore": {
                            "instrument": {
                                "price": {
                                    "last": 10678.8,
                                    "change": -716.99,
                                    "changePcr": -6.29,
                                }
                            }
                        }
                    }
                }
            }
        }
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            f"{json.dumps(payload)}</script>"
        )
        with patch(
            "regional_report.parsers.fetch",
            return_value=SimpleNamespace(text=html),
        ):
            item = parse_instrument_page(
                "https://www.investing.com/indices/kospi-50", "KOSPI 50", "KOSPI"
            )["KOSPI"]
        self.assertEqual(item["close"], "10678.8")
        self.assertEqual(item["source"], "KOSPI 50")
        self.assertEqual(REQUESTED_SOURCE_BY_KEY["KOSPI"], "KOSPI 50")

    def test_sunsirs_ammonia_daily_series(self):
        html = """
            <ul class="zwd_table">
              <li class="zwd_table_li"><p>Commodity</p><p>Price</p><p>Date</p></li>
              <li class="zwd_table_li"><p>Liquid ammonia</p><p>2340.00</p><p>06/28</p></li>
              <li class="zwd_table_li"><p>Liquid ammonia</p><p>2400.00</p><p>06/27</p></li>
            </ul>
        """
        with patch(
            "regional_report.parsers.fetch",
            return_value=SimpleNamespace(text=html),
        ):
            item = parse_ammonia()["Ammonia"]
        self.assertEqual(item["close"], "2340.00")
        self.assertEqual(item["change"], "-60.00")
        self.assertEqual(item["change_pct"], "-2.50%")
        self.assertEqual(item["previous_date"], "06/27")
        self.assertEqual(item["unit"], "RMB/ton")
        self.assertEqual(item["source"], "SunSirs")
        self.assertEqual(REQUESTED_SOURCE_BY_KEY["Ammonia"], "SunSirs")

    def test_jisdor_has_no_thousands_separator(self):
        html = """
            <table>
              <tr><td>Rp17.962,00</td></tr>
              <tr><td>Rp17.942,00</td></tr>
            </table>
        """
        with patch(
            "regional_report.parsers.fetch",
            return_value=SimpleNamespace(text=html),
        ):
            item = parse_jisdor()["Jisdor"]
        self.assertEqual(item["close"], "17962")
        self.assertEqual(item["change"], "+20")
        self.assertEqual(item["change_pct"], "+0.11%")

    def test_sunsirs_woodpulp_daily_change(self):
        html = """
            <table>
              <tr><th>Commodity</th><th>Sectors</th><th>06-25</th><th>06-26</th><th>Change</th></tr>
              <tr><td>Wood pulp</td><td>Building materials</td><td>4800.00</td><td>4783.33</td><td>-0.35%</td></tr>
            </table>
        """
        with patch(
            "regional_report.parsers.fetch",
            return_value=SimpleNamespace(text=html),
        ):
            item = parse_sunsirs_woodpulp()["Woodpulp"]
        self.assertEqual(item["close"], "4783.33")
        self.assertEqual(item["change"], "-16.67")
        self.assertEqual(item["change_pct"], "-0.35%")
        self.assertEqual(item["date"], "06-26")
        self.assertEqual(item["previous_date"], "06-25")

    def test_standard_format_includes_unchanged_daily_moves(self):
        data = {
            "Woodpulp": {
                "close": "4783.33",
                "change": "+0.00",
                "change_pct": "+0.00%",
            },
            "Ammonia": {
                "close": "2340.00",
                "change": "+0.00",
                "change_pct": "+0.00%",
                "note": "SunSirs (06/28)",
            },
        }
        report = format_report(data)
        self.assertIn("- **Woodpulp:** 4783.33 +0.00 +0.00%", report)
        self.assertIn("- **Ammonia:** 2340.00 +0.00 +0.00%", report)
        self.assertNotIn("SunSirs (06/28)", report)

    def test_invalid_market_news_does_not_create_empty_section(self):
        report = format_report({}, market_news=[{}, {"title": "Missing URL"}])

        self.assertNotIn("Market News Summary", report)

    def test_idx_property_is_not_fetched_twice(self):
        with patch(
            "regional_report.parsers.parse_yahoo_finance", return_value={}
        ) as yahoo:
            parse_yahoo_sector_indices()

        requested_codes = [call.args[1] for call in yahoo.call_args_list]
        self.assertNotIn("IDX Property", requested_codes)

    def test_indonesia_cds_uses_previous_daily_quote(self):
        payload = {
            "success": True,
            "result": {
                "ultimoValore": "90.00",
                "change1mAbs": "-7.50",
                "quote": {
                    "1": {"DATA_VAL": "2026-06-26", "CLOSE_VAL": 88.0},
                    "2": {"DATA_VAL": "2026-06-27", "CLOSE_VAL": 89.0},
                    "3": {"DATA_VAL": "2026-06-28", "CLOSE_VAL": 90.0},
                },
            },
        }
        item = parse_indonesia_cds_payload(payload)["IndoCDS 5yr"]
        self.assertEqual(item["close"], "90.00")
        self.assertEqual(item["change"], "+1.00")
        self.assertEqual(item["change_pct"], "+1.12%")
        self.assertEqual(item["previous_date"], "2026-06-27")
        self.assertNotEqual(item["change_pct"], payload["result"]["change1mAbs"])

    def test_gold_whatsapp_layout(self):
        data = {
            "Gold": {"close": "4043.50", "change": "+27.10", "change_pct": "+0.67%"},
            "Gold(Spot)": {
                "close": "4024.1400",
                "change": "+19.51",
                "change_pct": "+0.49%",
            },
        }
        report = format_report_whatsapp(format_report(data))
        self.assertIn("• *Gold:* 4043.50 +27.10 +0.67%", report)
        self.assertIn("• *Gold:* 4024.1400 +19.51 +0.49%", report)
        self.assertNotIn("❗", report)
        self.assertIn("     (XAU/USD)", report)

    def test_tlkm_idr_equivalent_is_shown_below_tlkm(self):
        data = {
            "TLKM": {
                "close": "16.06",
                "change": "-0.04",
                "change_pct": "-0.28%",
            },
            "Jisdor": {"close": "15,853"},
        }
        report = format_report_whatsapp(format_report(data))
        self.assertIn("• *TLKM:* 16.06 -0.04 -0.28%", report)
        self.assertIn("        (2546)", report)


if __name__ == "__main__":
    unittest.main()
