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

    def test_valid_daily_skips_fetch(self):
        fetch = Mock()
        self.assertEqual(daily_close([100.0, 101.5], fetcher=fetch), 101.5)
        fetch.assert_not_called()

    def test_latest_null_does_not_use_previous_day(self):
        with self.assertRaisesRegex(ValueError, REASON):
            daily_close([101.5, None], retries=0)

    def test_malformed_latest_does_not_use_previous_day(self):
        with self.assertRaisesRegex(ValueError, REASON):
            daily_close([101.5, "bad"], retries=0)

    def test_retry_zero_never_fetches(self):
        fetch = Mock()
        with self.assertRaisesRegex(ValueError, REASON):
            daily_close([None], fetcher=fetch, retries=0)
        fetch.assert_not_called()

    def test_retry_one_exactly_once(self):
        fetch = Mock(return_value=[None])
        with self.assertRaisesRegex(ValueError, REASON):
            daily_close([None], fetcher=fetch, retries=1, sleep=lambda _: None)
        self.assertEqual(fetch.call_count, 1)

    def test_retry_bound_rejects_above_two(self):
        with self.assertRaisesRegex(ValueError, "INVALID_RETRY_BOUND"):
            daily_close([None], retries=3)

    def test_retry_bound_rejects_negative(self):
        with self.assertRaisesRegex(ValueError, "INVALID_RETRY_BOUND"):
            daily_close([None], retries=-1)

    def test_retry_accepts_final_valid_value(self):
        fetch = Mock(side_effect=[[None], [300.0]])
        self.assertEqual(daily_close([None], fetcher=fetch, sleep=lambda _: None), 300.0)

    def test_retry_rejects_malformed_replacement(self):
        fetch = Mock(side_effect=[["bad"], [float("nan")]])
        with self.assertRaisesRegex(ValueError, REASON):
            daily_close([None], fetcher=fetch, sleep=lambda _: None)
        self.assertEqual(fetch.call_count, 2)

    def test_daily_rejects_nonpositive_latest(self):
        with self.assertRaisesRegex(ValueError, REASON):
            daily_close([100.0, -1.0], retries=0)

    def test_daily_accepts_numeric_string(self):
        self.assertEqual(daily_close(["100.25"], retries=0), 100.25)

    def test_intraday_rejects_wrong_symbol(self):
        stamp = int(datetime(2026, 10, 20, 19, 55, tzinfo=timezone.utc).timestamp())
        response = Mock()
        response.json.return_value = {"chart":{"result":[{"meta":{"symbol":"QQQ"},
            "timestamp":[stamp],"indicators":{"quote":[{"close":[503.5]}]}}]}}
        with self.assertRaisesRegex(ValueError, "SYMBOL_MISMATCH"):
            completed_close("SPY", "2026-10-20", datetime(2026,10,21,18,tzinfo=timezone.utc),
                            get=Mock(return_value=response))
