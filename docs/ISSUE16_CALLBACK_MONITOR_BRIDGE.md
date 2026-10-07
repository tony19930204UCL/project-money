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
