import pytest
from cio_market_lab.engine.leveraged_etf_sim import LeveredEtfSpec, simulate_nav, decay_vs_naive

Z = dict(annual_expense=0.0)

def test_single_day_exact():
    n = simulate_nav(LeveredEtfSpec("TQQQ", 3.0, **Z), [0.01])
    assert n[-1] == pytest.approx(1.03)

def test_volatility_decay_flat_underlying():
    r = [0.10, -1/11] * 20          # underlying returns to ~start each pair
    d = decay_vs_naive(LeveredEtfSpec("TQQQ", 3.0, **Z), r)
    assert d["underlying_return"] == pytest.approx(0.0, abs=1e-9)
    assert d["true_return"] < 0 and d["path_decay"] < 0

def test_inverse_decay():
    d = decay_vs_naive(LeveredEtfSpec("SQQQ", -3.0, **Z), [0.10, -1/11] * 20)
    assert d["true_return"] < 0

def test_wipeout_floor():
    n = simulate_nav(LeveredEtfSpec("X", 3.0, **Z), [-0.4, 0.1, 0.1])
    assert n[1] == 0.0 and n[-1] == 0.0

def test_fee_drag():
    n = simulate_nav(LeveredEtfSpec("X", 2.0, annual_expense=0.0252), [0.0] * 252)
    assert n[-1] == pytest.approx((1 - 0.0252/252) ** 252, rel=1e-9)

def test_bad_input():
    with pytest.raises(ValueError):
        simulate_nav(LeveredEtfSpec("X", 3.0), [-1.0])


def test_presets_leverage_signs():
    from cio_market_lab.engine.leveraged_etf_sim import PRESETS
    assert PRESETS["00631L.TW"].leverage == 2.0 and PRESETS["00632R.TW"].leverage == -1.0
    assert PRESETS["TQQQ"].leverage == 3.0 and PRESETS["SQQQ"].leverage == -3.0
