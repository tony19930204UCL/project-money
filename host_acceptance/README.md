# Installed-host acceptance

These tests are intentionally outside the default pytest `testpaths`. They are
acceptance proofs for the already-installed Project Money / Hermes host and are
not portable source-regression tests.

## Command

Run on the installed host only:

```sh
python -m pytest host_acceptance
```

## Preconditions

- The real Hermes installation is present and discoverable by the production launcher.
- The host's accepted Hermes interpreter is Python 3.14.
- The legacy Python 3.11 Hermes venv and installed Python 3.14 environment needed by
  the negative cross-ABI proof are present where the production discovery logic expects them.
- `/usr/bin/python3` and the installed bootstrap environment satisfy the original host proof.
- Use an isolated scratch `HERMES_HOME`; do not provide credentials or invoke paid inference.

## Migrated inventory

The following original modules moved without weakening their test names or assertions:

- `tests/test_cli_import_repair_regression.py` -> `host_acceptance/test_cli_import_repair_regression.py`
  - `test_hermes_launcher_resolves_installed_agent_and_python`
  - `test_negative_missing_cli_failure_reproduction`
  - `test_safe_no_inference_subprocess_proves_installed_cli_import`
  - `test_isolated_scratch_sentinel_never_leaked`
- `tests/test_native_abi_repair_regression.py` -> `host_acceptance/test_native_abi_repair_regression.py`
  - `test_hermes_python_resolves_pm_installed_venv_interpreter`
  - `test_safe_production_bootstrap_help_under_system_python_parent`
  - `test_native_abi_bootstrap_and_pydantic_core_import`
  - `test_legacy_311_cross_abi_negative_reproduction`
  - `test_isolated_scratch_sentinel_never_leaked`

That is **9 installed-host tests**. They are preserved as host acceptance and are
not replaced by fixture tests. Portable source tests exercise launcher selection
logic only and must not be cited as evidence that these installed-host proofs passed.

The migration removes these 9 tests from default source collection. Collection
counts for the revised source suite and host suite are recorded in the PR delivery
evidence from the latest CI/host runs.
