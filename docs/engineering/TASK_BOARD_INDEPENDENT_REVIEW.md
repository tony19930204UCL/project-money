# Task Board independent source audit (2026-10-15)

Scope: PR53 review branch, inherited PR51 source at d85411b05934dfb952278b00c1dfddf4a36a71fa. READ-ONLY inspection of GitHub source; no tests executed, no runtime data accessed. Recommendation: **CHANGES REQUIRED; do not accept source yet**. Findings below are source-proven unless explicitly marked hypothetical.

## Source-proven defects

1. **HIGH — configured relay indirection permits unrestricted local reads.** `task_board.py:project` accepts each `relay_plan.jobs[]` string as an arbitrary JSON file, then trusts its `state_dir` and reads three files under it. No root containment, canonical path checks or symlink restrictions. Counterexample (SYNTHETIC): relay_plan jobs points to `/tmp/other/job.json` whose state_dir is `/tmp/other`; this reads outside the configured relay-plan directory. The API does not return raw file contents, reducing direct exfiltration, but normalized fields may leak text. Require explicitly configured allowed roots, resolved-path containment, symlink policy and fail-closed diagnostics for out-of-root paths. Top-level configured sources also follow symlinks; define whether that is intended and test it.

2. **HIGH — privacy allowlist applies to keys, not values.** `normalize` passes arbitrary scalar `reason`, `latest_result`, `accept`, `owner`, `project`, `executor_ref`, and `text` to browser. Counterexample: `reason="token=synthetic-secret"` becomes `latest_result`; `handle.ref` may contain a personal path or command. Existing `test_secret_not_projected` only covers disallowed *keys*, not secrets inside allowed values. Implement strict per-field semantics, redaction/validation and conservative omission of free-form private evidence; do not rely on textContent for privacy.

3. **MEDIUM — source errors can masquerade as successful empty data.** `project` adds an `OK` diagnostic immediately after loading a dict/list, then `loaded[source].get(key, [])` silently interprets absent keys as zero records. Non-list/invalid item dictionaries are skipped without per-record diagnostics. Counterexample: `{"unexpected":[]}` for commitment_ledger produces OK and zero tasks. Validate source-specific schema before OK and report partial/invalid counts.

4. **MEDIUM — duplicate link discards disagreement.** `project` removes the obligation task when `registry_link.obligation_id` matches and emits only LINKED. Counterexample: linked commitment `state=VERIFIED` versus obligation `state=OPEN` loses disagreement without warning. Emit lifecycle conflict diagnostic and retain auditable source provenance.

5. **MEDIUM — task-board UI presents misleading successful refresh and lost error state.** `task-board.js:refresh` overwrites `last` with any HTTP-200 JSON, without validating tasks/projects/diagnostics; `render` can then throw, caught as fetch failure after corrupting last-valid snapshot. On subsequent filter input, `render` clears the `errors` element, removing a prior fetch-error warning even though no successful refresh occurred. Counterexample: fetch fails, user types search, error disappears while old data remains. Validate before assignment; store fetch error separately from source diagnostics and render persistently until recovery.

6. **MEDIUM — roadmap view does not expose acceptance conditions or unmapped tasks.** `task-board.js:render` roadmap branch shows stage state/scope/task IDs but omits `acceptance_conditions` projected by backend. Tasks without project/stage appear only as '未歸類' in task view; no unassigned roadmap group. This impairs explicit verification and discovery. Stage state is appropriately not inferred from child completion.

7. **MEDIUM — status selector misclassifies accepted and running-but-blocked records.** `status(t)` prioritizes blocker, then running, then scheduled, then lifecycle. A `DONE` source-accepted task with `awaiting_acceptance` is filterable as DONE, but `SOURCE_ACCEPTED` or `VERIFIED` awaiting acceptance is excluded from '完成待驗收'. A scheduled task whose lifecycle is BLOCKED is counted in both summary categories. Filter by derived acceptance/attention flags; make summary categories explicitly overlapping or exclusive.

8. **LOW — source timestamp freshness absent.** `project` exposes `polled_at` but no per-source freshness, stale threshold, or event age. UI shows '最近成功讀取' even for old artifacts. Separate polling health from source activity freshness. This overlaps implementation lane and is recorded for acceptance coverage.

9. **MEDIUM — relay and roadmap malformed structures can fail whole API.** `project` loops `relay.get("jobs", [])`, `roadmap.get("projects", [])`, `p.get("stages", [])` without validating list shape; unexpected scalar values can raise outside local error handling. `do_GET` catches and emits generic 503, losing per-source diagnostic isolation. Counterexample: roadmap `{"projects":null}`. Validate schema and preserve healthy sources.

## Security properties supported by source (not runtime certified)

`serve` rejects non-loopback host strings; GET routes are exact allowlist; POST/PUT/DELETE/PATCH return 405; CSP, nosniff and no-store headers are emitted; JS uses textContent for untrusted strings. No explicit mutation endpoint or CORS wildcard seen. These do not prove protection from localhost cross-origin request abuse, symlink escapes or privacy leakage.

## Hypothetical concerns requiring targeted tests

- ThreadingHTTPServer has no request concurrency/body/time budgets or explicit GET response length limits for static assets. Test resource exhaustion with bounded synthetic requests before deployment.
- `read_json` uses is_file/stat/open separately (TOCTOU). An adversary with local write access could swap symlink targets between checks; test adversarial local filesystem behavior and decide threat model.
- `localhost` DNS resolution may vary; verify actual bound address and IPv4/IPv6 semantics in host environment.
- `timestamp` converts timezone-naive ISO using host local timezone; verify required interpretation and display timezone.
- GET accepts path encodings only via exact route comparison; encoded traversal currently appears rejected, but no adversarial URL fuzz testing was executed.

## Test gaps / NOT_RUN

No pytest, Node harness, HTTP probe, or CI was executed in this review. Existing `tests/test_task_board_ui.py` executes real JS but only checks initial text safety, search and roadmap click. It does **not** exercise rejected fetch, retained snapshot/error warning after input, recovery, stale activity, keyboard navigation or status/project filters. Backend tests cover POST/PUT/DELETE but not PATCH/OPTIONS/HEAD, symlink chains, out-of-root relay job paths, secret-bearing allowed field values or malformed nested roadmap. All findings are source-level; no observed exploit execution is claimed.

## Acceptance decision

**REQUEST CHANGES (source review only).** Prioritize privacy projection of allowed fields, relay path containment/symlink policy, invalid-schema fail-visible diagnostics, fetch-error persistence and roadmap acceptance visibility. Re-review exact implementation SHA after fixes and execute targeted synthetic negative HTTP/Node tests. The separate live-schema/PID/freshness lane must be independently accepted; neither this report nor passing tests constitutes original-host verification, deployment or trading authorization.
