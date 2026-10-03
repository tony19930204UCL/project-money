# Source-only handoff acceptance — 2026-10-03

## Verified scope

- Original local candidate: **651 Python tests passed**, zero failed/skipped. This run included local acceptance harness dependencies that are deliberately not published.
- Source-only export, fresh Python 3.13 virtual environment: **604 Python tests passed**, zero failed/skipped.
- Frontend: `npm ci --ignore-scripts`, production build and Vitest run succeeded. **603 passed, 2 pending, 0 failed**. Pending frontend cases are not counted as passes.
- Application/plugin/strategy source copied from the local candidate. Frontend source restored from the attributed upstream source tree; compiled candidate assets are not committed. This does not assert byte-for-byte parity of generated bundles or re-prove original desktop/UI live acceptance.
- Secret scan and source manifest are verified locally before upload. Git starts from a new source-only baseline; original history is not pushed.

## Deliberate exclusions

The difference between 651 original and 604 exported Python tests is **47 locally dependent tests**, not 47 failing tests. They depend on private/local acceptance drivers, captured source artifacts or authenticated model-session logs. Shared portable tests remain in the export.

Excluded whole test modules:

- `tests/test_b01_slot_completion_20261002.py`
- `tests/test_b10_reconstructed_position_currency.py`
- `tests/test_b11_standard_mixed_boot.py`
- `tests/test_b13_explicit_ack_parent.py`
- `tests/test_daily_plan_driver_status_contract.py`
- `tests/test_daily_research_plan_integration.py`
- `tests/test_historical_fx_reporting_20261001.py`
- `tests/test_open_us_acceptance_harness.py`

Excluded individual captured-data cases:

- `tests/test_source_aligned_next_bar.py::test_captured_public_chart_stays_stale_and_cannot_be_live_execution`
- `tests/test_paper_runtime_integration.py::test_stream_parser_robustness_against_auxiliary_title_warning`

The original files and their acceptance evidence remain local. The export is not a substitute for those live/captured-source checks.

## Original acceptance remains open

- P5 had 20 local read-only browser cases and 48 GET receipts. It did **not** close original desktop receipt, live PAPER fill/position correspondence, live stream reconnection, mixed-version recovery or elapsed campaign requirements.
- P6 proved a scoped official-source-to-authenticated-CIO research receipt. It did **not** prove continuous deployed intake, full original source/strategy/UI parity or complete supported derivative live scenarios.
- Reddit source access remains blocked: recheck on 2026-10-03 returned HTTP 403. No challenge bypass, synthetic Reddit research or claimed acceptance.
- B15 elapsed observation/maturity remains open. Passage of time, live market/source events and separately authorized deployment cannot be manufactured by passing unit tests.

**This delivery is a tested private source baseline and contribution workflow. It is not complete original-system acceptance, main-service deployment or broker/live-money authorization.**


## Portable process/stream regression coverage

The source tree includes an offline integration test for B14/C09 that seeds only explicit `TEST_ONLY` data in a pytest temporary runtime, starts uvicorn in a real subprocess on loopback with an OS-assigned port, and exercises graceful restart, committed-state abrupt restart, HTTP event readback, SSE `Last-Event-ID` resume, WebSocket `since_id` resume, and read-only byte preservation. The test also checks that restart does not create duplicate `ORDER_FILLED` events or lose committed cash, positions, pending orders, experiment settings, or the test learning receipt.

This engineering coverage does **not** close the original B14/C09 production/live-source acceptance. It does not prove deployed-process recovery, real broker/source correspondence, live stream reconnection across production infrastructure, desktop behavior, or any ABC live acceptance item. Main must perform those local/live checks separately under the existing acceptance boundary.


## Source regression vs installed-host acceptance

The portable default pytest suite and installed-host acceptance are now explicit,
separate surfaces. The two original installed-machine modules
`test_cli_import_repair_regression.py` and `test_native_abi_repair_regression.py`
were moved intact to `host_acceptance/`, outside default `testpaths`. Their 9
original tests retain the real-Hermes, Python 3.14, legacy ABI, system-Python and
credential-containment assertions. Main must run `python -m pytest host_acceptance`
on the installed host; source CI does not claim those proofs.

Portable launcher tests under `tests/` use only pytest-owned fixture paths and
cover configured agent/interpreter selection, facts.json discovery priority,
missing-install fallback, invalid configured-path fallback, bootstrap-before-
entrypoint command construction, and current-interpreter pydantic/pydantic_core
imports. These are source regressions only and are not substitutes for installed-
host acceptance.

The B14/C09 integration now resumes both SSE and WebSocket cursors **across real
process boundaries**: a nonzero cursor is captured from the original PID, that
process is gracefully stopped or abruptly killed after committed fixture setup,
and a new PID using the same runtime must return exactly the committed HTTP suffix
through SSE `Last-Event-ID` and WebSocket `since_id`. Zero-cursor full replay,
finite timeouts, cleanup, sequence integrity, and read-only byte preservation remain
covered.
