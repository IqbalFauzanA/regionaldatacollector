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


if __name__ == "__main__":
    unittest.main()
