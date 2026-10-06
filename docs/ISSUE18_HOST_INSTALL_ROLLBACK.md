# Issue #18 host installation and rollback

This pull request is **source/tests only**. It must not be copied onto an installed host until Main CIO independently accepts the exact PR HEAD.

## Reviewable source mapping

After merge and explicit Main CIO deployment authorization, the reviewed repository files map 1:1 to the installed host paths:

- `custom_scripts/obligation_deadline_registry.py`
- `custom_scripts/client_execution_claim_gate.py`

The implementer does not know or require the host's private repository/runtime root. The deployer should resolve these paths inside the installed Project Money source checkout already used by the host.

No canonical obligation JSON, account data, receipts, credentials, scheduler configuration, cron, broker state, or private runtime payload belongs in this PR.

## Pre-install acceptance

On an isolated checkout of the exact approved HEAD:

```bash
python -m pytest -q tests/test_issue18_obligation_governance.py
python -m pytest
```

For a sanitized registry fixture, the source interfaces are:

```bash
python custom_scripts/obligation_deadline_registry.py --registry /path/to/sanitized-registry.json validate
python custom_scripts/obligation_deadline_registry.py --registry /path/to/sanitized-registry.json acceptance-check --id SANITIZED_ID
python custom_scripts/client_execution_claim_gate.py --registry /path/to/sanitized-registry.json --manifest /path/to/sanitized-manifest.json
```

Host acceptance is separate. Main CIO must run the same validator against the actual installed canonical registry and run a separate `acceptance-check` for every assigned canonical obligation. Source/fixture CI does not establish live host acceptance.

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
6. Preserve all original deadlines, FAILED/CLOSED dispositions and historical acceptance results.

If a reviewed migration candidate was generated but not installed, discard the candidate. If Main later authorizes a data migration, its rollback must restore the exact before-image identified by the migration report's `before_sha256`; that authorization is outside this PR.

## Non-claims

This PR does not deploy, extend deadlines, reopen retired lanes, convert FAILED/CLOSED/CANCELLED to PASS, infer missing completion timestamps, promote a local route to origin delivery, or claim client-visible completion from a structurally valid late recovery.
