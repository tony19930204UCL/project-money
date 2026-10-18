from cio_market_lab.engine.exit_learning import predict_exit_horizon, scan_exit_horizons


def rows():
    return [
        {"sequence": i, "hold_ticks": 10 + (i % 3) * 10,
         "pnl_by_horizon": {"10": 1.0 + i * 0.1, "20": 1.5 + i * 0.1, "30": 0.2}}
        for i in range(8)
    ]


def test_prediction_is_deterministic_and_retrospective_only():
    result = predict_exit_horizon(rows()[:5], candidates=(10, 20, 30), min_samples=2)
    assert result["selected_horizon"] == 20
    assert result["evidence_scope"] == "RETROSPECTIVE_ONLY"
    assert result["promotion_ready"] is False


def test_scan_uses_prior_rows_and_requires_oos_gate():
    result = scan_exit_horizons(rows(), candidates=(10, 20, 30), min_train_samples=3, min_oos_samples=3)
    assert result.selected_horizon == 20
    assert result.selected_oos_mean_pnl is not None
    assert result.evidence_scope == "RETROSPECTIVE_ONLY"
    assert result.promotion_ready is True


def test_prediction_rejects_missing_horizon_evidence():
    bad = [{"sequence": 1, "hold_ticks": 10, "pnl_by_horizon": {"10": 1.0}}]
    try:
        predict_exit_horizon(bad, candidates=(20,), min_samples=1)
    except ValueError as exc:
        assert "missing pnl_by_horizon[20]" in str(exc)
    else:
        raise AssertionError("missing horizon evidence must fail closed")
