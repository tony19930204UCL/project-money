"""PAPER ONLY warrant pricing regression tests."""
from datetime import date, timedelta
import pytest
from cio_market_lab.engine.tw_warrant_sim import (
    WarrantSpec, bs_price, implied_vol, greeks, warrant_tick,
    issuer_quote, daily_mark_path, settlement,
)

TODAY = date(2026, 10, 10)
EXP = TODAY + timedelta(days=365)


def spec(right="CALL"):
    return WarrantSpec("TEST01", "2330.TW", right, 100.0, EXP, 0.1, "SIM_ISSUER")


def test_ratio_and_put_call_parity():
    c, p = spec(), spec("PUT")
    diff = bs_price(c, 110, TODAY, .02, .3, .01)-bs_price(p, 110, TODAY, .02, .3, .01)
    import math
    assert diff == pytest.approx(.1*(110*math.exp(-.01)-100*math.exp(-.02)))


def test_iv_round_trip():
    s = spec()
    px = bs_price(s, 105, TODAY, .02, .32, .01)
    assert implied_vol(s, px, 105, TODAY, .02, .01) == pytest.approx(.32, abs=1e-6)


def test_iv_no_solution():
    assert implied_vol(spec(), 1000, 100, TODAY, .02) is None


@pytest.mark.parametrize("price,tick", [(4.99,.01),(5,.05),(49.99,.05),(50,.1),
    (99.99,.1),(100,.5),(499.99,.5),(500,1),(999.99,1),(1000,5)])
def test_tick_ladder(price,tick):
    assert warrant_tick(price) == tick


def test_expiry_cash_settlement():
    assert settlement(spec(), 130, EXP) == pytest.approx(3)
    assert settlement(spec("PUT"), 80, EXP) == pytest.approx(2)
    assert bs_price(spec(), 130, EXP, .02, .3) == pytest.approx(3)


def test_post_expiry_rejection():
    with pytest.raises(ValueError):
        bs_price(spec(), 100, EXP+timedelta(days=1), .02, .3)
    with pytest.raises(ValueError):
        implied_vol(spec(), 1, 100, EXP+timedelta(days=1), .02)


def test_theta_negative_long_call():
    assert greeks(spec(), 100, TODAY, .02, .3)["theta_per_day"] < 0


def test_greeks_scale_with_ratio():
    s = spec()
    g = greeks(s, 100, TODAY, .02, .3)
    assert g["delta"] > 0 and g["gamma"] > 0 and g["vega"] > 0
    assert g["delta"] < .1


def test_issuer_quote_spread_and_tick_boundary():
    q = issuer_quote(4.995, 1)
    assert q["bid"] == 4.99 and q["ask"] == 5.05


def test_daily_mark_path():
    path = daily_mark_path(spec(), [(TODAY,100),(TODAY+timedelta(days=1),102)], .02, .3)
    assert len(path) == 2 and path[1]["model_price"] > path[0]["model_price"]


def test_invalid_spec_and_vol():
    with pytest.raises(ValueError):
        spec("OTHER")
    with pytest.raises(ValueError):
        bs_price(spec(), 100, TODAY, .02, -.1)
