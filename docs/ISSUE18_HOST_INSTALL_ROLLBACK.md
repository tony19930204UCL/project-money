# Issue #18 host installation, canonical compatibility, and rollback

This pull request is **source/tests/docs only**. It must not be copied onto an installed host until Main CIO independently accepts the exact PR HEAD.

## Reviewable source mapping

After merge and explicit Main CIO deployment authorization, the reviewed repository files map 1:1 to the installed host paths:

- `custom_scripts/obligation_deadline_registry.py`
- `custom_scripts/client_execution_claim_gate.py`

The implementer does not know or require the host's private repository/runtime root. No canonical obligation JSON, account data, receipts, credentials, scheduler configuration, cron, broker state, or private runtime payload belongs in this PR.

## Canonical installed-registry compatibility

The installed canonical shape may use:

- `obligation_id` instead of `id`;
- `acceptance_criteria` / `acceptance_evidence` instead of `original_criteria` / `criteria_evidence`;
- `execution_one_shot` instead of `execution_history` + `executor_binding`;
- `deadline` + `deadline_source_ref` instead of `original_deadline` + `original_deadline_source_ref`;
- a top-level `executor_readback` object instead of inline `live_jobs`.

`adapt_canonical_registry_snapshot` performs a **nonmutating projection** into the validator's internal schema. It deep-copies the source document, preserves all source fields, never overwrites original deadlines or failure history, and does not invent a missing schedule, source reference, completion timestamp, live job, or criterion result.

A canonical `execution_one_shot` is only projected when its source-backed fields are present. Missing `source_ref`, deadline source, live schedule/state, or criterion evidence remains a validation failure. The adapter report includes before/after SHA-256 and `input_unchanged=true` for the original source object.

The claim-gate CLI applies the same nonmutating adapter before evaluating an execution manifest.

## Existing CLI command compatibility

The validator preserves the installed command verbs:

```bash
python3 custom_scripts/obligation_deadline_registry.py validate
python3 custom_scripts/obligation_deadline_registry.py acceptance-check --id <id>
```

For source checkout / sanitized fixtures, the canonical registry path can be supplied by:

```bash
export PROJECT_MONEY_OBLIGATION_REGISTRY=/path/to/sanitized-registry.json
python3 custom_scripts/obligation_deadline_registry.py validate
python3 custom_scripts/obligation_deadline_registry.py acceptance-check --id SANITIZED_ID
```

or explicitly in either compatible argument order:

```bash
python3 custom_scripts/obligation_deadline_registry.py --registry /path/to/sanitized-registry.json validate
python3 custom_scripts/obligation_deadline_registry.py validate --registry /path/to/sanitized-registry.json
```

Optional source-backed live executor readback can be supplied with `--live-jobs` or `PROJECT_MONEY_LIVE_JOBS`. If neither is present in the canonical payload nor supplied externally, the validator does not fabricate it; active obligations that require it remain blocked.

The claim gate remains:

```bash
python3 custom_scripts/client_execution_claim_gate.py --registry /path/to/sanitized-registry.json --manifest /path/to/sanitized-manifest.json
```

## Scheduling and deadline semantics

Original schedule and original deadline are independent immutable facts.

- The first execution-history item must be `ORIGINAL_SCHEDULE` with its own source reference.
- `original_deadline` must have its own source reference.
- A legitimate original schedule may be before the deadline; equality is not required.
- An original schedule after the deadline is invalid.
- An ordinary active executor must satisfy `now < scheduled_at <= original_deadline`.
- The actual live executor readback must match job/run/model/provider/route/state and, for ordinary execution, the exact scheduled time.
- Disabled/non-executable jobs cannot satisfy ordinary registration.
- Late recovery is represented separately and never rewritten into an on-time schedule or deadline extension.

## Identity and claim semantics

A duplicate canonical obligation ID makes the named case identity ambiguous. `acceptance-check --id` returns BLOCKED for that ID even if one duplicate record is COMPLETED/PASS. Unrelated structural errors elsewhere remain separately visible through global schema status and do not automatically rewrite a unique named criterion outcome.

`FULL_MAIN_CIO` is an explicit authenticated canonical scope. It must:

- match a canonical scope ID, authorization source, exact live executor binding, and owner;
- include every Main CIO obligation assigned by that full-owner scope, including relevant terminal outcomes;
- contain at least one assigned obligation;
- provide exactly one manifest result per assigned obligation;
- provide actual completion/effect evidence for PASS or source-backed failure evidence for FAIL.

An empty result set can never pass by vacuous `all([])`. A scope with zero assigned obligations remains noncompletion. `FAILED`, `CLOSED`, and `CANCELLED` remain non-success terminal outcomes and cannot be converted into PASS.

## Pre-install acceptance

On an isolated checkout of the exact approved HEAD:

```bash
python -m pytest -q tests/test_issue18_obligation_governance.py
python -m pytest
```

Host acceptance is separate. Main CIO must run the actual installed canonical registry through `validate` and then run a separate `acceptance-check --id ...` for every assigned canonical obligation. Source/fixture CI does not establish live host acceptance.

## Installation procedure (Main-owned)

1. Record the approved Git commit SHA.
2. Compute SHA-256 for the two currently installed source files and preserve a read-only backup outside canonical runtime data.
3. Copy only the two reviewed source files from the approved commit into the corresponding installed source paths.
4. Do **not** migrate or overwrite canonical registry/runtime data during source installation.
5. Run `validate` first. If it is not `DEADLINE_REGISTRY_VALID`, stop and rollback the source files.
6. Run `acceptance-check --id ...` separately for every assigned obligation. A globally valid schema does not imply a per-case PASS.
7. If migration is needed, first run the pure migration helper against a copy/export and review its before/after hashes, reversible diff, source refs, deadline invariance and failure invariance. Never use mtime, current time, scheduled time or reviewed_at as a fabricated completion timestamp.
8. Run the execution claim gate only with an authenticated canonical execution scope and exact live executor binding.

## Rollback

Rollback is source-only unless Main separately authorizes a reviewed data migration.

1. Stop further host acceptance commands.
2. Restore the two prior source files from the preserved backups.
3. Verify their SHA-256 values match the pre-install hashes.
4. Re-run the old validator only for diagnostic comparison.
5. Do not rewrite canonical obligation data to make old source accept new semantics.
6. Preserve all original deadlines, original schedules, FAILED/CLOSED dispositions and historical acceptance results.

If a reviewed migration candidate was generated but not installed, discard the candidate. If Main later authorizes a data migration, its rollback must restore the exact before-image identified by the migration report's `before_sha256`; that authorization is outside this PR.

## Non-claims

This PR does not deploy, extend deadlines, rewrite original schedules, reopen retired lanes, convert FAILED/CLOSED/CANCELLED to PASS, infer missing completion timestamps, promote a local route to origin delivery, or claim client-visible completion from a structurally valid late recovery.
