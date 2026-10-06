# Issue #18 compatibility adapters, review path, and rollback boundary

This pull request is **source/tests/docs only**. It does not replace or install the host's existing scripts.

## Namespaced delivery

To avoid breaking installed interfaces, Issue #18 now ships reviewed adapters under separate names:

- `custom_scripts/issue18_obligation_compat.py`
- `custom_scripts/issue18_client_claim_compat.py`

The previously proposed same-name replacements for the installed registry/claim scripts were removed from this PR. Main CIO's read-only host review established that the installed registry CLI also exposes `register` / `complete` and default registry discovery, while the installed client claim gate accepts a positional manifest plus optional `--root`. Those installed entry points remain untouched.

There is **no 1:1 copy instruction** from this PR onto installed host scripts.

## Canonical registry compatibility

The read-only registry adapter accepts sanitized shape-equivalent forms of the canonical installed signatures:

- `obligation_id -> id`;
- `acceptance_criteria -> original_criteria`;
- `acceptance_evidence -> criteria_evidence`;
- `execution_one_shot.run_at -> execution_history[0].scheduled_at` and active executor binding schedule;
- `deadline/deadline_source_ref -> original_deadline/original_deadline_source_ref`;
- top-level `executor_readback` as live-job readback when `live_jobs` is absent.

Canonical statuses are preserved in `canonical_status` and mapped only to validated internal semantic equivalents:

- `REGISTERED -> PENDING`;
- `IN_PROGRESS -> ACTIVE`;
- `OVERDUE -> RECOVERY`.

Unknown status values remain unsupported. `OVERDUE` does not become a synthetic success: if its original deadline has passed, the existing evidence-backed recovery contract is still mandatory.

The adapter is nonmutating: it deep-copies the source object, preserves canonical originals, reports before/after SHA-256, and never invents a missing run time, source reference, completion timestamp, criterion result, recovery authorization, or live executor.

Canonical alias adaptation is fail-closed. If both sides of an alias pair are present, the adapter must prove semantic equivalence before normalization. Conflicting `id/obligation_id`, `deadline/original_deadline`, `deadline_source_ref/original_deadline_source_ref`, `original_criteria/acceptance_criteria`, `criteria_evidence/acceptance_evidence`, or schedule aliases are retained in diagnostics and block validation; no preferred alias silently wins. Complete evidence mappings must agree exactly; a PASS-valued alias cannot override conflicting canonical evidence. Timestamp aliases may differ textually only when they resolve to the same instant.

Acceptance criteria are also count/order preserving. String criteria and mappings with one unambiguous `criterion_id`/`id`/`name` are supported; malformed or ambiguously named entries block adaptation instead of being discarded. Thus a mandatory criterion can never disappear merely because the adapter cannot name it.

## Completion-time semantics

A source-backed completion timestamp is necessary but not sufficient. `completed_at` must also satisfy:

```
completed_at <= observed validation time
```

A completion in the future is `COMPLETION_IN_FUTURE` and cannot pass named acceptance or an execution claim. Claim evaluation passes one explicit observation clock through to named acceptance so registry and claim decisions cannot disagree about whether completion has occurred.

## Executor and scope semantics

Ordinary active registration still requires:

```
observed_now < scheduled_at <= original_deadline
```

For canonical host fields, `run_at` is the source of the normalized schedule.

At both ordinary execution and authenticated scope boundaries, job metadata is not accepted merely because copies match. Required fields must be present/non-null:

- job ID;
- run ID;
- model;
- provider;
- route kind;
- state.

The live state must be executable: `ENABLED`, `SCHEDULED`, `ACTIVE`, or `READY`. `DISABLED` is rejected even when canonical scope, presented manifest scope, and live readback all say `DISABLED`.

If a schedule/run_at is present in the canonical scope binding, the live readback must match it semantically. A binding that itself carries conflicting `scheduled_at` and `run_at` is rejected at every canonical-scope, presented-scope, and live-readback boundary, even when all three copies carry the same contradiction.

## Existing installed CLI / manifest compatibility boundary

The installed registry CLI is **not replaced**. Therefore its existing `register`, `complete`, default discovery, and any other installed verbs remain available exactly as they are.

The namespaced registry adapter is invoked explicitly for read-only review:

```bash
python3 custom_scripts/issue18_obligation_compat.py --registry /path/to/sanitized-registry.json validate
python3 custom_scripts/issue18_obligation_compat.py --registry /path/to/sanitized-registry.json acceptance-check --id SANITIZED_ID
```

Its migration command is preview-only:

```bash
python3 custom_scripts/issue18_obligation_compat.py --registry /path/to/sanitized-registry.json migrate-preview --source-refs /path/to/sanitized-source-refs.json
```

The namespaced client-claim adapter preserves the installed **invocation shape** for a positional manifest and optional root:

```bash
python3 custom_scripts/issue18_client_claim_compat.py /path/to/manifest.json --root /path/to/sanitized-root
```

For the namespaced adapter only, `--registry` can be used instead of `--root`.

The adapter also accepts sanitized legacy manifest aliases without mutating the input:

- top-level `scope -> execution_scope`;
- top-level `results -> obligation_results`;
- row `obligation_id -> id`;
- row `result -> acceptance_result` for explicit PASS/FAIL.

Unknown/missing fields remain fail-closed. Legacy aliases are compatibility inputs, not precedence rules. Raw alias trees are validated on both sides before any semantic normalization or comparison, including shadow-only `scope` / `results` branches and nested executor bindings. Alias **absence** is distinct from alias **presence**: an optional legacy key that is genuinely absent may use the documented fallback path, while a key that is present with `null`, empty/whitespace text, an invalid scalar/container type, or an out-of-domain value is not treated as absent and blocks the complete claim. Identity aliases must be nonempty strings; result aliases must be explicit PASS/FAIL; schedule aliases must be timezone-aware parseable instants; top-level scope aliases must be mappings; top-level result aliases must be lists whose rows are mappings. An internally contradictory branch (for example conflicting `scheduled_at/run_at`) blocks even if normalization would otherwise make it look equal to the selected branch. If both `scope/execution_scope`, `results/obligation_results`, row `id/obligation_id`, or row `result/acceptance_result` are present, both branches must first pass their own domains and then be provably equivalent or the complete claim is blocked. Both originals are preserved for review and the input object remains unchanged.

## FULL_MAIN_CIO and scoped claims

A completion claim requires an authenticated canonical scope, exact assigned IDs, executable live binding, and actual per-case evidence.

- `FULL_MAIN_CIO` includes all obligations represented by its canonical full-owner scope, including terminal outcomes.
- zero assigned obligations cannot pass;
- empty results cannot pass;
- every submitted result row is authoritative input and must identify exactly one case through a valid `id` or legacy `obligation_id`, and must carry an explicit valid `acceptance_result` or legacy `result`; unidentified, identity-only, outcome-only, malformed, unresolved, or otherwise incomplete rows are never filtered or ignored;
- selected and shadow result lists are validated row-for-row before alias normalization, and direct evaluator calls repeat the same row-completeness checks so callers cannot bypass them by skipping the adapter;
- result rows map 1:1 to submitted cases: duplicate or unassigned result IDs are blocked, and no supplied FAIL/unresolved row may disappear from accounting;
- terminal FAIL/CLOSED/CANCELLED outcomes cannot become PASS;
- PASS requires canonical named acceptance plus completion and verified effect evidence;
- source-backed FAIL evidence can be recorded only as noncompletion.

## Source-only review commands

On an isolated checkout of the exact PR HEAD:

```bash
python -m pytest -q tests/test_issue18_obligation_governance.py
python -m pytest
```

The focused tests use only sanitized fixtures derived from field/state/interface signatures reported by Main CIO. They do not upload private host records.

## Future integration path

After exact-HEAD source acceptance, Main CIO may independently decide whether and how to integrate the namespaced adapters with installed host tooling.

A safe integration review would:

1. keep the existing installed registry/claim scripts intact;
2. feed a read-only export or in-memory canonical object into the namespaced adapter;
3. compare adapter outcomes with installed command behavior;
4. separately validate every named obligation;
5. authorize any host wiring only in a later deployment-specific change.

This PR does not authorize that wiring.

## Rollback boundary

Because this PR does not overwrite installed host scripts or data, source rollback is simply reverting/removing the namespaced adapter files from the repository before any later deployment integration.

Any separately authorized future integration must preserve:

- original deadlines;
- original schedule/run_at facts;
- historical FAILED/CLOSED/CANCELLED dispositions;
- original acceptance outcomes;
- source-backed completion evidence;
- existing installed CLI verbs and manifest invocation behavior.

## Non-claims

This PR does not deploy, mutate installed runtime/data, extend deadlines, rewrite run_at/schedules, reopen retired lanes, convert historical failure into PASS, infer a missing artifact, install cron/workers, or claim live-host acceptance.
