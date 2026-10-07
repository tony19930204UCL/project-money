# Issue #16 original-engine callback and local monitor bridge

This change is source/tests only. It does not deploy, alter runtime schedules,
change providers, touch broker state, or publish private holdings/contracts.

## Research bridge boundary

The reviewed bridge is implemented in
`cio_market_lab/research/issue16_acceptance.py` and is exposed through
`OriginalResearchEngineCallbackBridge`.

It does not replace the original research engine. Instead it supplies the
existing caller-owned callback seam:

- `fetch(symbol, now, reader)`
- `generate(stage, payload, now)` for discovery/commercial/underwriting
- `challenge(payload, now)`
- `verdict(underwriting, challenge)`

The host entrypoint remains caller-owned and is injected into
`run_original_entrypoint(...)`.

Every outbound worker payload is projected through the existing public-only
research sanitizer. Holdings, orders, accounts, credentials and private paths
are rejected before transport and again at stage output.

## Route contract

Each stage uses a `StageRouteContract` with one explicit primary route and at
most two explicit fallbacks. Every configured route must declare
`is_free_or_local_authorized=true`.

Every attempted route still runs through the existing Hermes subprocess and
runtime verifier and therefore requires:

- affirmative authenticated runtime evidence;
- non-fixture runtime evidence;
- actual integer returncode equal to 0;
- resolved provider/model matching the pinned route;
- stage-specific schema-valid JSON.

Blocked primary/fallback attempts are retained in `route_attempts`. Missing
routes or all-blocked routes remain BLOCKED. There is no paid-route escalation.

The challenge verdict can only pass when underwriting is
`PUBLIC_EVIDENCE_READY` and the challenge is produced by a different verified
provider/model identity. INCOMPLETE and REJECT cannot be promoted.

## Main-owned research candidate invocation

On the installed host, Main may provide a JSON file containing only route
contracts (no credentials) and the import path of the original engine
entrypoint:

```bash
python3 custom_scripts/issue16_bridge_candidate.py research \
  --symbol MSFT \
  --routes /path/to/issue16-routes.json \
  --entrypoint installed_research_engine:run_live_callbacks
```

The route JSON has this shape:

```json
{
  "discovery": {"primary": {"provider":"...","model":"...","session_id":"...","workspace_root":"...","is_free_or_local_authorized":true,"purpose":"..."}, "fallback":[]},
  "commercial": {"primary": {"provider":"...","model":"...","session_id":"...","workspace_root":"...","is_free_or_local_authorized":true,"purpose":"..."}, "fallback":[]},
  "underwriting": {"primary": {"provider":"...","model":"...","session_id":"...","workspace_root":"...","is_free_or_local_authorized":true,"purpose":"..."}, "fallback":[]},
  "challenge": {"primary": {"provider":"...","model":"...","session_id":"...","workspace_root":"...","is_free_or_local_authorized":true,"purpose":"..."}, "fallback":[]}
}
```

Credentials remain in the installed runtime/provider environment and are never
stored in this repository or route file.

## Local position/receipt bridge

`LocalPositionReceiptBridge` joins caller-supplied local monitor contracts to
the existing `ReceiptAwarePositionConsumer` and existing
`DeliveryReceiptConsumer`.

The bridge preserves:

- contract ID;
- condition ID and version;
- stable observation identity;
- canonical quote freshness/stale/unknown behavior;
- sibling-condition independence;
- VTI contract coverage;
- restart-stable pending replay;
- exact receipt execution/body/job/platform/target/thread linkage;
- fixture/generation/failed/unknown delivery as non-ACK.

Pending delivery records and PLATFORM_ACK records share the same existing
`CIOSessionHistory`. No second ACK store is created.

Main may run a local audit with private contract files that never enter GitHub:

```bash
python3 custom_scripts/issue16_bridge_candidate.py monitor \
  --contracts /private/local/contracts.json \
  --quotes /private/local/current-quotes.json \
  --history-root /private/local/cio-history \
  --session-id issue16-host-monitor
```

Optional receipt linkage:

```bash
python3 custom_scripts/issue16_bridge_candidate.py monitor \
  --contracts /private/local/contracts.json \
  --quotes /private/local/current-quotes.json \
  --history-root /private/local/cio-history \
  --expected /private/local/expected-linkage.json \
  --receipt /private/local/platform-receipt.json
```

## Source acceptance commands

```bash
python -m pytest -q tests/test_issue16_research_acceptance.py
python -m pytest
```

Browser/frontend regressions remain part of repository CI.

## Live acceptance boundary

CI and source tests do not prove a real free/local provider route, fresh official
input, local private-contract join or authentic platform receipt. If the
engineering environment lacks those authorizations, the source PR must report
that as an objective Main-owned host acceptance blocker and provide the commands
above. No fixture may substitute for live acceptance.

## Rollback

Rollback is source-only: revert this PR. No installed runtime or canonical data
is changed by this review.
