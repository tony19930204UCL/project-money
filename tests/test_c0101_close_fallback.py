import unittest
from datetime import datetime, timezone
from unittest.mock import Mock
from cio_market_lab.engine.paper_close_fallback import completed_close, daily_close, REASON

class CloseFallbackTests(unittest.TestCase):
    def test_same_session_final_bar(self):
        stamp = int(datetime(2026, 10, 20, 19, 55, tzinfo=timezone.utc).timestamp())
        response = Mock()
        response.json.return_value = {"chart":{"result":[{"meta":{"symbol":"SPY"},"timestamp":[stamp],"indicators":{"quote":[{"close":[512.5]}]}}]}}
        get = Mock(return_value=response)
        self.assertEqual(completed_close("SPY", "2026-10-20", datetime(2026,10,21,18,tzinfo=timezone.utc),get=get),512.5)
        self.assertEqual(get.call_count,1)

    def test_future_or_incomplete_rejected_without_request(self):
        get = Mock()
        with self.assertRaisesRegex(ValueError,"NOT_COMPLETED"):
            completed_close("SPY","2026-10-21",datetime(2026,10,21,18,tzinfo=timezone.utc),get=get)
        get.assert_not_called()

    def test_stale_intraday_rejected(self):
        stamp = int(datetime(2026, 10, 19, 19, 55, tzinfo=timezone.utc).timestamp())
        response = Mock()
        response.json.return_value = {"chart":{"result":[{"meta":{"symbol":"SPY"},"timestamp":[stamp],"indicators":{"quote":[{"close":[512.5]}]}}]}}
        with self.assertRaisesRegex(ValueError,"NO_SAME_SESSION"):
            completed_close("SPY","2026-10-20",datetime(2026,10,21,18,tzinfo=timezone.utc),get=Mock(return_value=response))

    def test_all_null_daily(self):
        with self.assertRaisesRegex(ValueError, REASON):
            daily_close([None, None], retries=0)

    def test_malformed_daily(self):
        for closes in ([], ['bad'], [float('nan')], [float('inf')], [0]):
            with self.subTest(closes=closes), self.assertRaisesRegex(ValueError, REASON):
                daily_close(closes, retries=0)

    def test_retry_bound(self):
        fetcher = Mock(return_value=[None])
        sleeper = Mock()
        with self.assertRaisesRegex(ValueError, REASON):
            daily_close([None], fetcher=fetcher, sleep=sleeper)
        self.assertEqual(fetcher.call_count, 2)
        self.assertEqual(sleeper.call_count, 2)

    def test_retry_recovers(self):
        fetcher = Mock(side_effect=[[None], [501.25]])
        self.assertEqual(daily_close([None], fetcher=fetcher, sleep=lambda _: None), 501.25)
        self.assertEqual(fetcher.call_count, 2)

    def test_intraday_fallback_after_daily_null(self):
        stamp = int(datetime(2026, 10, 20, 19, 55, tzinfo=timezone.utc).timestamp())
        response = Mock()
        response.json.return_value = {"chart":{"result":[{"meta":{"symbol":"SPY"},
            "timestamp":[stamp],"indicators":{"quote":[{"close":[503.5]}]}}]}}
        with self.assertRaisesRegex(ValueError, REASON):
            daily_close([None], fetcher=Mock(return_value=[None]), sleep=lambda _: None)
        self.assertEqual(completed_close("SPY", "2026-10-20",
            datetime(2026,10,21,18,tzinfo=timezone.utc), get=Mock(return_value=response)), 503.5)
