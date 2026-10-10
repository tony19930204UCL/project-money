from cio_market_lab.data import official_archive as oa

def test_archive_and_crosscheck(tmp_path):
    rows = {"twse": [{"Date": "1151008", "Code": "2330", "ClosingPrice": "2,550.00", "OpeningPrice": "2560", "HighestPrice": "2570", "LowestPrice": "2540", "TradeVolume": "100"}, {"Date": "1151008", "Code": "X", "ClosingPrice": "--"}],
            "tpex": [{"Date": "1151008", "SecuritiesCompanyCode": "6488", "Close": "400", "Open": "1", "High": "1", "Low": "1", "TradingShares": "5"}]}
    fetch = lambda url: rows["twse" if "twse" in url else "tpex"]
    s = oa.archive_today(tmp_path, fetch)
    assert s["twse"]["count"] == 1 and s["tpex"]["count"] == 1
    assert oa.crosscheck(2560, 2550) and not oa.crosscheck(3000, 2550)
    assert (tmp_path / "twse_1151008.json").exists()
    assert oa.archive_today(tmp_path, fetch)["twse"]["count"] == 1  # idempotent
