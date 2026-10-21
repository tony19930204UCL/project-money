import unittest
from datetime import datetime, timezone
from unittest.mock import Mock
from cio_market_lab.engine.paper_close_fallback import completed_close

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
