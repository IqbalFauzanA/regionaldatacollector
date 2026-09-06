import unittest
from threading import BoundedSemaphore
from types import SimpleNamespace
from unittest.mock import patch

from regional_report.commons import fetch


class FetchTests(unittest.TestCase):
    def test_retry_backoff_does_not_hold_host_semaphore(self):
        semaphore = BoundedSemaphore(1)
        blocked = SimpleNamespace(status_code=403)
        success = SimpleNamespace(status_code=200)
        acquired_during_backoff = []

        def inspect_semaphore(_delay):
            acquired = semaphore.acquire(blocking=False)
            acquired_during_backoff.append(acquired)
            if acquired:
                semaphore.release()

        with (
            patch("regional_report.commons._get_host_semaphore", return_value=semaphore),
            patch(
                "regional_report.commons.req.get",
                side_effect=[blocked, blocked, success],
            ),
            patch("regional_report.commons.time.sleep", side_effect=inspect_semaphore),
        ):
            self.assertIs(fetch("https://example.com", max_retries=2), success)

        self.assertEqual(acquired_during_backoff, [True])

    def test_fetch_raises_on_http_202_waf_challenge(self):
        waf_resp = SimpleNamespace(
            status_code=202,
            text="<script>window.awsWafCookieDomainList=['barchart.com'];</script>",
        )
        with (
            patch("regional_report.commons.req.get", return_value=waf_resp),
            patch("regional_report.commons.time.sleep"),
        ):
            with self.assertRaises(Exception) as ctx:
                fetch("https://www.barchart.com/test", max_retries=1)
            self.assertIn("HTTP 202 (AWS WAF Challenge)", str(ctx.exception))

    def test_fetch_raises_on_non_200_status(self):
        err_resp = SimpleNamespace(status_code=500, text="Internal Server Error")
        with (
            patch("regional_report.commons.req.get", return_value=err_resp),
            patch("regional_report.commons.time.sleep"),
        ):
            with self.assertRaises(Exception) as ctx:
                fetch("https://example.com", max_retries=1)
            self.assertIn("HTTP 500", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
