# Issue #16 original callback + local monitor bridge

This PR is a **source/tests-only engineering candidate**. It does not deploy, change cron/provider configuration, mutate canonical registries, access a broker, export holdings, or claim Main-owned live acceptance.

## Original research-engine callback mapping

`cio_market_lab.research.issue16_live_bridge.OriginalResearchCallbackBridge` is glue around the existing research modules; it is not a second research engine. The host/original research entrypoint remains responsible for the DAG. The bridge injects three callbacks:

- `fetch_callback`: calls the existing `FreeSourceCoordinator`, seals only public evidence, requires an official public source, and records public URL/hash/observed time.
- `generate_callback(stage, payload)`: supports only discovery, commercial, and underwriting; each stage uses its existing strict schema and an explicit primary/fallback `StageRoute`.
- `challenge_callback(payload, underwriting_model_identity)`: uses the challenge schema and blocks unless the authenticated resolved challenge model is genuinely different from the underwriting model **and model family**.

Each inference path keeps the actual subprocess evidence surfaced by `HermesLocalInference`: resolved provider/model, explicit `auth_verified=true`, `is_success_response=true`, non-fixture state, and real integer return code 0. Missing routes, unauthorized routes, invalid metadata/schema, private outbound material, or a same-model challenge remain BLOCKED.

The bridge never supplies paid-route authority. The caller must construct `InferenceContract.is_free_or_local_authorized=true` only from the existing authorized runtime contract.

### Candidate host invocation

PR #28 is the authoritative Issue #16 candidate. PR #27 is a superseded
overlapping candidate, not a second research engine or deployment lane.

This PR includes a runnable candidate CLI and maps the host signature actually
reported by Main:

`run_case(case_id,ticker,seed_urls,directory,fetch,generate,challenge,max_attempts)`

```bash
python3 custom_scripts/issue16_bridge_candidate.py research \
  --symbol MSFT \
  --case-id issue16-main-acceptance \
  --seed-url https://www.sec.gov/example \
  --directory /local/main-owned/research-case \
  --routes /local/main-owned/issue16-routes.json \
  --entrypoint REAL_INSTALLED_MODULE:run_case \
  --max-attempts 2
```

`REAL_INSTALLED_MODULE:run_case` is deliberately supplied by Main. The
repository does not invent a private host module. Main also supplies the actual
local directory, credentials and host authorization. If the module/adapter is
missing or callback shapes differ, the candidate remains BLOCKED with an exact
next action.

The route JSON contains only free/local route contracts, never credentials. Each stage must also declare the model family used for independence checks; a fallback must declare its own family, and a primary/fallback pair from the same family is rejected before inference:

```json
{
  "discovery": {"primary": {"provider":"...","model":"...","session_id":"...","workspace_root":"...","is_free_or_local_authorized":true,"purpose":"..."}, "primary_model_family":"family-a", "fallback": null},
  "commercial": {"primary": {"provider":"...","model":"...","session_id":"...","workspace_root":"...","is_free_or_local_authorized":true,"purpose":"..."}, "primary_model_family":"family-b", "fallback": null},
  "underwriting": {"primary": {"provider":"...","model":"...","session_id":"...","workspace_root":"...","is_free_or_local_authorized":true,"purpose":"..."}, "primary_model_family":"family-c", "fallback": null},
  "challenge": {"primary": {"provider":"...","model":"...","session_id":"...","workspace_root":"...","is_free_or_local_authorized":true,"purpose":"..."}, "primary_model_family":"family-d", "fallback": null}
}
```

### Inference timeout boundary

The original-host inference route catches `subprocess.TimeoutExpired` at the bridge boundary so a timeout cannot escape into the host as a raw command-bearing exception. A timed-out attempt is retained as a typed fail-closed callback attempt with only public-safe primitive fields: stage, route, configured provider/model, declared model family, timeout seconds, stdout/stderr presence booleans, and stdout/stderr byte counts.

The bridge never copies the timeout exception's raw command, stdout/stderr content, prompt, workspace, or local paths into public results or callback evidence. Without an authorized fallback, the stage returns the deterministic reason `HOST_<STAGE>_INFERENCE_TIMEOUT`. If an explicitly configured existing fallback is present, the normal bounded route loop may continue only under the existing free/local authorization and model-family rules; a timeout never manufactures runtime authentication/success receipts and never turns an unsuccessful attempt into PASS.

### Underwriting task/output source contract alignment

The original host callback arity remains unchanged. Before calling the existing underwriting inference route, the bridge now makes two previously implicit constraints explicit inside the underwriting task payload:

- `required_output_fields_for_pass`: the exact underwriting output fields required for PASS.
- `allowed_source_urls`: the exact sanitized seed URL allowlist supplied by the original host `run_case`.

This removes an ambiguity where the model was required to return `source_urls` / `independent_source_mix` only from host seed URLs even though the underwriting task did not explicitly expose that allowlist, and where the contract mislabeled output requirements as payload requirements. If the bridge cannot supply a non-empty allowed source list, underwriting fails closed before inference. Existing post-output seed-subset validation remains authoritative, so the explicit allowlist does not weaken source validation or create evidence.

### Strict underwriting schema visibility

The original-host callback signature remains unchanged: `generate("underwriting", payload)` receives the host-provided `documents + discovery + commercial` task input, and challenge remains a separate callback. This repair does not redesign the host DAG or change stage ordering.

The underwriting JSON schema shown to the inference route now exposes the same PASS contract that the bridge already enforced after inference. Its generated schema contains a conditional PASS rule requiring every field in `HOST_UNDERWRITING_REQUIRED_FIELDS`, while `INCOMPLETE` and `REJECT` may still omit facts that public evidence cannot support. Field descriptions explicitly instruct the model not to invent unsupported values and to use `INCOMPLETE` when evidence is insufficient.

This closes a source contract gap where the prompt's generated JSON schema previously advertised underwriting fields as optional even though the host/bridge would reject or downgrade an incomplete PASS afterward. It does **not** claim that the live 120-second underwriting latency is eliminated; runtime timeout handling remains fail-closed and Main must re-run the real host underwriting case for acceptance.

### Underwriting PASS semantics

The original-host underwriting bridge now treats `PASS` as a complete, evidenced state rather than accepting the model's status label at face value. The underwriting prompt payload explicitly lists the required fields for PASS:

- `financials`
- `market_metrics`
- `capital_structure`
- `independent_source_mix`
- `reflexivity_score`
- `scenario_return_estimates`
- `factor_labels`
- `business_maturity`
- `valuation_scenarios`
- `buy_zone`
- `invalidation_conditions`
- `review_by`
- `four_sentences`

The prompt instructs the model to use only supplied public documents and seed URLs, never invent missing numerical values, and return `INCOMPLETE` with `MISSING_UNDERWRITING_FIELDS:<names>` when evidence is insufficient. As a second fail-closed boundary, the bridge independently downgrades any model-labeled `PASS` with missing required fields to `INCOMPLETE` and preserves the missing fields by name. A remaining `PASS` must also carry a non-empty `independent_source_mix` whose URLs are a subset of the supplied seed URLs.

This specifically covers the Microsoft official-earnings false-PASS observed during Main acceptance: discovery/commercial output may exist, but underwriting cannot progress to challenge merely because the model returned `PASS` while the required financial/market/capital/source-mix/reflexivity/scenario/factor fields are absent.

Successful stage callbacks retain resolved provider/model plus affirmative auth,
non-fixture and successful-returncode evidence. Missing adapter/route,
unauthorized inference, schema failure, private output or same-model challenge
stays BLOCKED. The tested success path remains fetch -> discovery -> commercial
-> underwriting -> genuinely heterogeneous model challenge -> verdict; fixed
rules only validate/reduce model outputs and do not replace model inference.

## Local position-contract -> receipt consumer mapping

`LocalPositionReceiptBridge` accepts four injected local-only dependencies:

1. a sanitized local contract-registry loader;
2. an existing quote provider;
3. a receipt provider;
4. the existing `CIOSessionHistory`.

It reuses `ReceiptAwarePositionConsumer` for quote-edge evaluation and `DeliveryReceiptConsumer` for ACK semantics. No second ACK store exists.

The sanitized contract carries only contract/condition/version/observation identity, symbol, the existing observation contract, and expected receipt linkage. Holdings/account fields are not part of the schema and private-field inspection remains fail-closed.

For triggered conditions the receipt must still match exact execution/body/job/platform/target/thread linkage and have a nonempty platform message ID. Fixture/generation-only/failed/mismatched evidence remains UNKNOWN.

When a trigger first lacks a genuine ACK, the existing `CIOSessionHistory`
stores one `MONITOR_PENDING_RECEIPT` row with the original contract,
condition/version/observation identity and an immutable sanitized snapshot of
only the exact expected execution/body/job/platform/target/thread linkage.
Every later `evaluate` replays unresolved pending rows through the same
`DeliveryReceiptConsumer` **before** current quote/registry evaluation. Thus a
stale, missing or out-of-zone current quote, or removal/change of the current
registry condition, cannot strand the original receipt. The new observation
identity is never applied to the old receipt. Historical replay cannot create a
new trigger or order.

A matching genuine receipt writes the canonical `PLATFORM_ACK` once and marks
that exact pending identity resolved; replay after resolution is idempotent.
There is no second ACK store.

The bridge preserves sibling conditions separately through `condition_id`, `condition_version`, and `observation_identity`; quote/poll time does not enter the stable contract identity. It reports FRESH, STALE, UNKNOWN, and research-only classifications while keeping `private_positions_exported=false`.

### Main-owned local invocation

The same CLI can perform a local-only monitor audit. Main supplies all private
paths locally; none are uploaded by this source PR:

```bash
python3 custom_scripts/issue16_bridge_candidate.py monitor \
  --contracts /private/local/contracts.json \
  --quotes /private/local/current-quotes.json \
  --history-root /private/local/cio-history \
  --session-id issue16-host-monitor \
  --receipt /private/local/platform-receipt.json
```

Main supplies the private local registry/holdings join and genuine receipt on
the host. Source tests use synthetic VTI contracts only and contain no client
holdings or real delivery evidence.

## Genuine Microsoft official-input intake

The installed-run_case fetch adapter uses the repository's existing `official_documents` parser. Raw acquisition is bounded by that parser's official-document byte budget, not by the 250 KB outbound text budget. Only parser-produced official text/tables that pass the existing public-output validator are joined into the exact host envelope `{url,text,observed_at}`. The isolated case evidence records source URL, observation time, raw content SHA-256, extracted content SHA-256, extraction success/failure, and parser-part diagnostics. Oversize, fetch, parse, nonofficial-host, private-content, or empty-extraction failures remain fail-closed.

A standalone source-CI probe exercises the genuine Microsoft FY2026 Q4 investor disclosure through the same `run_installed_run_case -> fetch` callback pathway without invoking any model:

```bash
python custom_scripts/issue16_public_intake_probe.py
```

The probe intentionally returns host terminal `INCOMPLETE` after successful fetch because inference is not invoked. It is public-input evidence only, never live inference acceptance.

## Source-only acceptance commands

Focused:

```bash
python -m pytest -q tests/test_issue16_research_acceptance.py tests/test_issue16_live_bridge.py
```

Full backend:

```bash
python -m pytest
```

Frontend/browser remain the repository's existing Source Regression workflow jobs and are not redefined here.

## Host/live limitation

A fresh official-input free/local full chain can only be claimed where the engineering environment has the installed authorized inference route and the original host entrypoint. GitHub source CI has neither authority nor credentials to fabricate that proof. Therefore green source CI means the callback/monitor bridge is ready for Main's independent exact-head host acceptance; it does **not** close the original live research/monitor obligations.
