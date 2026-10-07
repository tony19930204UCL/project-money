# Issue #16 original callback + local monitor bridge

This PR is a **source/tests-only engineering candidate**. It does not deploy, change cron/provider configuration, mutate canonical registries, access a broker, export holdings, or claim Main-owned live acceptance.

## Original research-engine callback mapping

`cio_market_lab.research.issue16_live_bridge.OriginalResearchCallbackBridge` is glue around the existing research modules; it is not a second research engine. The host/original research entrypoint remains responsible for the DAG. The bridge injects three callbacks:

- `fetch_callback`: calls the existing `FreeSourceCoordinator`, seals only public evidence, requires an official public source, and records public URL/hash/observed time.
- `generate_callback(stage, payload)`: supports only discovery, commercial, and underwriting; each stage uses its existing strict schema and an explicit primary/fallback `StageRoute`.
- `challenge_callback(payload, underwriting_model_identity)`: uses the challenge schema and blocks unless the authenticated resolved challenge model is genuinely different from the underwriting model.

Each inference path keeps the actual subprocess evidence surfaced by `HermesLocalInference`: resolved provider/model, explicit `auth_verified=true`, `is_success_response=true`, non-fixture state, and real integer return code 0. Missing routes, unauthorized routes, invalid metadata/schema, private outbound material, or a same-model challenge remain BLOCKED.

The bridge never supplies paid-route authority. The caller must construct `InferenceContract.is_free_or_local_authorized=true` only from the existing authorized runtime contract.

### Candidate host invocation

The original host entrypoint is deliberately injected rather than imported by name because source CI does not contain the installed original host module:

```python
from datetime import datetime, timezone
from cio_market_lab.research.issue16_live_bridge import (
    OriginalResearchCallbackBridge, StageRoute,
)
# Build StageRoute objects from already-authorized free/local HermesLocalInference
# contracts, then pass the installed/original callable:
result = bridge.run_original_entrypoint(
    installed_original_research_entrypoint,
    symbol="MSFT",
    now=datetime.now(timezone.utc),
)
```

If the installed callable is absent or does not expose `fetch_callback`, `generate_callback`, and `challenge_callback`, the candidate returns an explicit host blocker. It never substitutes fixture success.

## Local position-contract -> receipt consumer mapping

`LocalPositionReceiptBridge` accepts four injected local-only dependencies:

1. a sanitized local contract-registry loader;
2. an existing quote provider;
3. a receipt provider;
4. the existing `CIOSessionHistory`.

It reuses `ReceiptAwarePositionConsumer` for quote-edge evaluation and `DeliveryReceiptConsumer` for ACK semantics. No second ACK store exists.

The sanitized contract carries only contract/condition/version/observation identity, symbol, the existing observation contract, and expected receipt linkage. Holdings/account fields are not part of the schema and private-field inspection remains fail-closed.

For triggered conditions the receipt must still match exact execution/body/job/platform/target/thread linkage and have a nonempty platform message ID. Fixture/generation-only evidence remains UNKNOWN. Unknown/failed receipt results are persisted only as `MONITOR_PENDING_RECEIPT` audit rows; after restart, a later genuine receipt is processed by the same `DeliveryReceiptConsumer` and the pending identity is marked resolved. This pending audit is not an ACK store.

The bridge preserves sibling conditions separately through `condition_id`, `condition_version`, and `observation_identity`; quote/poll time does not enter the stable contract identity. It reports FRESH, STALE, UNKNOWN, and research-only classifications while keeping `private_positions_exported=false`.

### Main-owned local invocation

```python
bridge = LocalPositionReceiptBridge(
    registry_loader=load_existing_local_contract_registry,
    quote_provider=load_existing_quote,
    receipt_provider=load_existing_platform_receipt,
    history=existing_cio_session_history,
)
audit = bridge.evaluate(now=datetime.now(timezone.utc))
```

Main supplies the private local registry/holdings join on the host. Source tests use synthetic VTI contracts only and contain no client holdings.

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
