<span class="eyebrow">Appendix F</span>

# Feature Flags in Lumen {.chtitle}

Lumen's wallet API can expose a new offer without changing its balance or ledger rules. The optional `/api/v1/wallets/rollout/offer` route demonstrates a runtime gate: the existing offer stays available while a new one is rolled out. The code and behavioral tests are in `samples/lumen`; the full API and key reference live in the [Feature Flags guide](https://github.com/fireflyframework/fireflyframework-pyfly/blob/v26.10.01/docs/modules/feature-flags.md), and the document format is the [Firefly contract](https://github.com/fireflyframework/fireflyframework-pyfly/blob/v26.10.01/docs/modules/feature-flags-contract.md) shared with LaraFly.

## F.1. Start with an off switch

Install the sample's local PyFly with `feature-flags` extra, then run its tests:

```bash
cd samples/lumen
uv sync --extra dev
uv run pytest tests/test_feature_flags.py -q
uv run pyfly run --server uvicorn
```

The sample's `pyfly.yaml` enables the subsystem and sets `wallet-offer: false`. A boolean shorthand is accepted in configuration and test overrides; a file, HTTP source or store requires a full definition. `FeatureFlags` is an injectable facade when enabled. Turning the subsystem off omits that bean, and a closed gate without a fallback raises `FeatureFlagDisabledException`.

The route uses `@feature_flag("wallet-offer", fallback="legacy_offer")`. The wrapper preserves the method's route mapping and signature. With the sample default, a request gets `{"offer":"standard"}`; with the flag on, it gets `{"offer":"new"}`. The fallback is a method on the same controller and does not modify the wallet aggregate. The sample test exercises the real gate and restores the previous provider after each `override_flags` scope. The route itself can be reached at `GET /api/v1/wallets/rollout/offer`.

Without a fallback, a closed mapped route answers `pyfly.feature-flags.web.disabled-status` (404 by default, also 403 or 503). A gate may set `default=True` to intentionally open on a missing, disabled or errored flag. The caller's default also governs a variant gate when no variant resolves; `DISABLED` does not always mean `false`.

## F.2. Define and target a flag

A portable definition names `state`, same-type `variants`, a `defaultVariant` and optional JSON Logic `targeting`. The state is `ENABLED` or `DISABLED`. A missing variant, type mismatch or failed evaluation yields the caller's typed default and a reason/error code in `details()`. `get_string`, `get_int`, `get_float`, `get_object` and `is_enabled` choose their type explicitly; `variant()` returns the assigned variant name. There are asynchronous counterparts for every evaluation method.

```yaml
wallet-experiment:
  state: ENABLED
  variants: {control: standard, treatment: new}
  defaultVariant: control
  targeting: {fractional: [[control, 50], [treatment, 50]]}
  metadata: {kind: experiment, owner: wallet, expires: "2026-12-31"}
```

The `fractional-v2` algorithm hashes the flag key and stable `targetingKey`, so a user stays in one cohort in PyFly and LaraFly. Anonymous traffic without a stable key takes the default. The sample's test reads the **same vendored conformance fixture** as both framework suites and checks Alice's multivariate value and variant through the real OpenFeature provider. For a permission rule, target a trusted `tenant` or `plan` attribute with JSON Logic. Shared `$evaluators` can be referenced with `{"$ref": "name"}`; missing or cyclic references make only the affected flag return `PARSE_ERROR`. Full validation and expansion limits are in the contract.

PyFly's ambient context includes the authenticated user ID, roles without `ROLE_`, application and profiles. The tenant comes from the principal attribute; the `X-Tenant-Id` header is used only when `context.trust-tenant-header` is enabled behind a trusted boundary. Explicit `context=` wins over ambient attributes, and `targeting_key=` wins over a context key. Text or decimal integer targeting keys normalize to text; invalid values leave a lower-precedence key intact. Custom `EvaluationContextContributor` beans add attributes such as a plan. Keep contributors inexpensive and idempotent: request setup and each facade call invoke them again.

## F.3. Choose a source and change it safely

Effective precedence, lowest to highest, is config, watched file, polled HTTP, polled store, then test overrides. A higher layer replaces a whole flag definition, while evaluator and document metadata names merge. Replace files atomically; the watcher detects mtime and size. Failed refreshes retain the last good document and mark the source `STALE`; a source with no successful load is `DOWN`. Invalid config/file definitions stop startup, but HTTP/store failures keep the last good composition. `FeatureFlagsChanged` reports actual effective changes.

Use the database store when operators must update flags across instances. The canonical flag row and audit change row are written in one transaction, with a monotonic change ID as the poll revision. `expectedVersion` rejects a stale write as `conflict`. The writing process refreshes after commit; peers refresh on their next interval. Inside a caller-owned transaction, refresh and `FeatureFlagUpdated` wait for the **outer commit** and do not run after rollback. Some native database conflicts abort that outer transaction; retry the whole caller operation with a fresh snapshot. See Chapter 5 for migrations and Chapter 18 for deployment. A memory store is useful locally but is process-local.

A writable store plus `management.writes: true` permits `put`, `delete`, `enable`, `disable` and `default-variant`. `FlagManagement.evaluate` previews with explicit context only: it ignores the operator's principal and records neither metric nor exposure. A successful put can return `{key, refreshPending: true}` while a caller-owned transaction or refresh has not made it locally visible; poll the GET view. The receipt alone is not a durability claim. The `/admin#flags` screen, the `GET/POST /actuator/flags/{key}` routes and `pyfly flags` use the same service. A missing GET key returns 404/`unknown-flag`; malformed POST data returns 400/`bad-request`. HTTP callers cannot select the trusted admin audit channel. Management authentication is separate from application security; configure it before exposing writes. See Chapter 15 for the actuator/admin and Appendix D for executable CLI usage.

## F.4. Share the document and measure rollout

A service may enable the flagd-compatible sync endpoint at `server.path` (default `/feature-flags/flagd.json`). Use a bearer token; `server.allow-anonymous` is false by default, and a blank or root path is invalid. The remote HTTP source sends `If-None-Match`; 304 keeps its cached document. Set positive refresh intervals and a timeout. When an application supplies its own OpenFeature provider, the facade and gate remain, but Firefly's composed definitions, store and sync endpoint stand down.

Every non-preview evaluation increments `feature_flag_evaluations_total{flag,variant,reason}`. Set `events.evaluations: true` for `FeatureFlagEvaluated` exposure events. Preview suppresses exposure. The snapshot budget inspects at most 10,000 value occurrences; larger values omit the exposure while evaluation and metrics continue. `metadata.expires` tracks flag debt in health/admin views but never disables a flag. See Chapter 15 for telemetry, Chapter 16 for overrides and assertions, and Chapter 14 for trusted identity/context.

## F.5. Verify the Lumen rollout

The following is the exact test from `samples/lumen/tests/test_feature_flags.py`. It exercises the checked-in controller and provider, including restoration after each override:

::: listing samples/lumen/tests/test_feature_flags.py | Listing F.1 — Off, on and missing definitions use the real gate
@pytest.mark.asyncio
async def test_wallet_offer_route_method_falls_back_and_activates() -> None:
    controller = WalletController(None, None)  # this route does not dispatch commands or queries
    with override_flags({"wallet-offer": False}):
        assert await controller.wallet_offer() == {"offer": "standard"}
    with override_flags({"wallet-offer": True}):
        assert await controller.wallet_offer() == {"offer": "new"}
    with override_flags({}):
        assert await controller.wallet_offer() == {"offer": "standard"}
:::

The same sample test file loads `firefly-vectors.json` for a shared experiment, drives the mapped route through ASGI, uses a real SQLite flag store for commit/conflict/rollback, and checks that preview records no exposure while an ordinary evaluation does. The framework's `tests/feature_flags` suite adds the wider backend and route-status matrix. The sample keeps Lumen's wallet lifecycle tests separate from its optional rollout. Leaving an override context restores the prior provider and registry state. A preview result is not an exposure record.

## F.6. Configuration and troubleshooting

`pyfly.feature-flags.enabled` defaults to false. `sources.file`, `sources.http` and `sources.store` each have `enabled` and a positive `refresh-interval`; HTTP adds URL, token and timeout, store adds driver and datasource. `context.tenant-attribute` defaults to `tenant`, `context.trust-tenant-header` to false, `web.disabled-status` to 404, `management.writes` and `events.evaluations` to false. `server` includes enabled, path, token and `allow-anonymous`; `openfeature.domain` isolates applications in a process. See the guide's [configuration reference](https://github.com/fireflyframework/fireflyframework-pyfly/blob/v26.10.01/docs/modules/feature-flags.md#configuration-reference) for exact keys.

If everyone gets the default variant, check the stable targeting key and the flag's state. If a change seems lost, inspect source health and precedence before rewriting it. A `conflict` requires a fresh version, and a native transaction abort requires a whole-operation retry. An expired flag still evaluates: remove or disable it deliberately. To avoid YAML 1.1 surprises in `pyfly.yaml`, quote `"on"` and `"off"` variant keys; quote ambiguous keys and dates in portable files.
