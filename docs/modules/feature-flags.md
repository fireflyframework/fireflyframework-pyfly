# Feature Flags Guide

PyFly evaluates [flagd](https://flagd.dev) definitions through OpenFeature. The same [Firefly contract](feature-flags-contract.md) governs PyFly and LaraFly documents, targeting and store rows. For a runnable wallet-service rollout, see [Lumen](https://github.com/fireflyframework/fireflyframework-pyfly/tree/main/samples/lumen).

## Quick start

Install the optional dependency and enable the subsystem:

```bash
uv add 'pyfly[feature-flags]'
```

```yaml
pyfly:
  feature-flags:
    enabled: true
    flags:
      new-checkout: false
      checkout-flow: v1
```

```python
from pyfly.feature_flags import FeatureFlags, feature_flag

@feature_flag("new-checkout")
def new_checkout() -> str:
    return "new"

def checkout_flow(flags: FeatureFlags) -> str:
    return flags.get_string("checkout-flow", "v1")
```

When disabled, no feature-flag provider or registry is configured. A closed gate raises `FeatureFlagDisabledException`; a facade injected from the feature-flag auto-configuration is unavailable. The `feature-flags` extra supplies OpenFeature and the flagd evaluator. Importing definitions, gating and test helpers alone does not require the extra.

## Defining flags

A full definition has `state`, nonempty same-type `variants`, optional `defaultVariant`, JSON Logic `targeting` and scalar `metadata`. `DISABLED`, missing flags, evaluation errors, and a definition without a matching/default variant return the **caller's typed default**. A disabled flag does not force `false` when the caller chose `true`.

```yaml
pyfly:
  feature-flags:
    enabled: true
    flags:
      new-checkout:
        state: ENABLED
        variants: {"on": true, "off": false}
        defaultVariant: "off"
        targeting:
          if: [{"in": ["beta", {"var": "roles"}]}, "on", null]
        metadata: {kind: release, owner: wallet, expires: "2026-12-31"}
    evaluators:
      beta: {"in": ["beta", {"var": "roles"}]}
```

Configuration and test overrides also accept boolean and string shorthand. Files, HTTP and store documents require full definitions. Quote YAML `on` and `off` keys in `pyfly.yaml`: its YAML 1.1 parser otherwise reads them as booleans. Quote dates and any ambiguous YAML keys for portable documents. The [contract](feature-flags-contract.md#flag-documents) lists exact validation rules, messages, `$ref` expansion limits and JSON portability.

## Targeting recipes

`targeting` is JSON Logic with flagd's `fractional`, `sem_ver`, `starts_with` and `ends_with` extensions. `$evaluators` rules can be reused by `{"$ref": "beta"}`. A missing or cyclic reference makes that flag evaluate with `PARSE_ERROR`; other flags remain usable.

A deterministic 10% rollout uses the flag key and a stable `targetingKey` in `fractional-v2` bucketing:

```yaml
new-search:
  state: ENABLED
  variants: {"on": true, "off": false}
  defaultVariant: "off"
  targeting: {"fractional": [["on", 10], ["off", 90]]}
```

Supply a stable authenticated user ID or another stable key. Anonymous requests without one take the default. A tenant entitlement can use `{"if": [{"==": [{"var": "tenant"}, "acme"]}, "on", "off"]}`. A multivariate experiment can use string variants such as `control: v1` and `treatment: v2`; call `variant()` to retain the assigned variant name. The shared conformance fixtures pin identical Python/PHP outcomes, including targeting-key normalization.

## Gating code

`@feature_flag(key, variant=None, default=False, fallback=None)` gates synchronous and asynchronous functions, methods, classes and mapped routes. A closed gate calls its fallback if supplied; otherwise it raises `FeatureFlagDisabledException`. Route responses use `pyfly.feature-flags.web.disabled-status` (404, 403 or 503) and `FEATURE_FLAG_DISABLED`.

```python
from pyfly.feature_flags import feature_flag

class WalletOffer:
    @feature_flag("new-checkout", fallback="legacy")
    def render(self) -> str:
        return "new"

    def legacy(self) -> str:
        return "legacy"
```

The gate's `default=True` opens a missing, disabled or failed flag deliberately. A variant gate matches the resolved variant, and still obeys the caller default if no variant resolves. The facade provides `is_enabled`, `get_string`, `get_int`, `get_float`, `get_object`, `variant` and `details`, with asynchronous counterparts. Typed getters convert the supplied default to their getter type. `details` infers the type from its default. Template functions `feature_flag` and `feature_variant` are added to views rendered through `ModelAndView` when templates are enabled. Runtime flags cannot determine which beans exist at startup; use `@conditional_on_property` for that.

## Evaluation context

The ambient context adds the authenticated principal's user ID as `targetingKey`, roles without `ROLE_`, a trusted tenant, application name and active profiles. Explicit `context=` overrides attributes; explicit `targeting_key=` wins over the context key. Nonempty string and decimal integer keys are accepted; other values are refused with a bounded DEBUG diagnostic, preserving a lower-precedence key. Preview uses only process attributes plus explicit context and removes the request's OpenFeature transaction context.

Implement `EvaluationContextContributor.contribute(attributes)` as a bean to add plan or account attributes. Contributors execute during request setup and again on facade evaluations; keep them inexpensive, idempotent and free of side effects. A principal tenant attribute wins. `X-Tenant-Id` is consulted only when `context.trust-tenant-header: true` and should be trusted only behind a verified boundary.

## Sources and precedence

From low to high: configuration (`flags`, `evaluators`), watched `file`, polled `http`, polled `store`, and test overrides. A higher source replaces a flag's whole definition; `$evaluators` and document metadata merge by name. File changes are detected by mtime and size: replace a complete file atomically rather than writing it in place. A failed refresh keeps the last good document (`STALE`); a source that never loaded is `DOWN`. Invalid config or file definitions fail startup; HTTP and store failures leave the application running with the last good composition.

An application-provided OpenFeature `AbstractProvider` bean supersedes Firefly's provider. The facade, gate and context still work; the Firefly registry, source composition, store, sync endpoint and definition views stand down. Do not assume Firefly-specific `variant()` type inference or management definitions are available with an external provider.

## The store

Enable a process-local store for development, or a database store for shared operation:

```yaml
pyfly:
  feature-flags:
    enabled: true
    sources:
      store:
        enabled: true
        driver: database
        datasource: ""
        refresh-interval: 5s
    management:
      writes: true
```

`database` needs the relational extra and the Firefly flag and change tables, shared with LaraFly. A write updates the definition row and appends an audit change in one transaction. `expectedVersion` detects stale operators. Other processes see committed changes on their next poll. Inside a caller-owned outer transaction, visibility, refresh and update events wait for its commit; rollback discards them. A native database conflict can abort that transaction: retry the **whole caller transaction** with a fresh snapshot, rather than continuing inside it. The [contract store section](feature-flags-contract.md#the-store) defines DDL, versioning and conflict rules.

## Managing flags at runtime

`FlagManagement` is the shared read, preview and write service behind actuator, admin and CLI. Reads describe effective origins, layers, versions and source health. `evaluate` is a preview with explicit context; it does not inherit the operator's principal, does not record a metric or exposure, and does not write. Writes require `management.writes: true` and a writable store. Actions are `put`, `delete`, `enable`, `disable` and `default-variant`; `expectedVersion` is optional. Error codes include `writes-disabled`, `not-writable`, `invalid-definition`, `unknown-flag`, `unknown-variant`, `conflict` and `bad-request`.

A successful put may return `{key, refreshPending: true}` when a caller-owned transaction has not committed or refresh cannot yet show the definition. Poll its GET view for visibility; the receipt alone is not a durability claim. A delete with no visible next layer returns `{key, deleted: true}`.

Expose the `flags` actuator endpoint to list flags at `GET /actuator/flags`, inspect one at `GET /actuator/flags/{key}`, or run an action at `POST /actuator/flags/{key}`. For example, `{"action":"evaluate","context":{"plan":"pro"},"targetingKey":"user-42"}` previews an assignment. A missing GET selector returns 404/`unknown-flag`; malformed, empty or non-object POST bodies return 400/`bad-request`. Other actuator management refusals return 400 with their portable error code. The admin page at `/admin#flags` shows definitions, sources, history and preview, and offers state, variant and JSON-definition controls. Its API uses its own 403/404/409/422/400 refusal statuses. An unauthenticated HTTP caller cannot choose the trusted `admin` audit origin: writes use the authenticated principal or the surface's server-selected actor.

The CLI supports local application boot or a running actuator with `--url`. It has `list`, `show`, `enable`, `disable`, `default-variant`, `put`, `delete` and `evaluate` commands. `--json` keeps refusals machine-readable and exits 1; an accepted pending write exits 0. Local writes record `cli:<os-user>`, while remote writes inherit the actuator actor. Management security is separate from application security; configure it before exposing writes. See the [CLI reference](../cli.md#pyfly-flags) for exact syntax and [Actuator](actuator.md) for endpoint exposure.

## Serving flags to other services

Enable the flagd-compatible sync endpoint with `server.enabled: true`, a non-root `server.path` (default `/feature-flags/flagd.json`) and a token. The server returns the effective normalized document, excluding test overrides, with a quoted SHA-256 ETag and `Cache-Control: no-cache`. It authenticates before evaluating `If-None-Match`; strong, weak, listed and wildcard matches return 304. HTTP clients send the ETag, bearer token and timeout and reuse their last good document on 304 or a failed refresh. A 304 before any accepted document is an error. If a valid response lacks an ETag, the client computes a quoted SHA-256 of its exact body bytes for its next request. `server.allow-anonymous` defaults to `false`. A blank or root path is refused at startup. Treat bearer tokens as secrets from the environment, and configure positive refresh intervals.

## Observability

`feature_flag_evaluations_total{flag,variant,reason}` counts non-preview evaluations; errors use variant `none`, reason `ERROR`. `FeatureFlagsChanged` reports effective set changes and `FeatureFlagUpdated` follows a committed store write. `events.evaluations: true` opts into `FeatureFlagEvaluated` exposure events. Preview suppresses exposures. Exposure snapshots inspect at most 10,000 value occurrences; above that, the exposure is omitted with a DEBUG diagnostic while evaluation and metrics continue. Health and the flags actuator surface source state and expired flag debt. `metadata.expires` is advisory and does not disable evaluation.

## Testing

`override_flags({"new-checkout": True})` works as a context manager or decorator and restores the prior provider/registry state on exit. The `feature_flags` pytest fixture provides per-test overrides. For integration tests, boot the actual application, use an isolated datasource and exercise the mapped route through `httpx.AsyncClient` with `ASGITransport` in the same event loop. [Lumen's rollout tests](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/samples/lumen/tests/test_feature_flags.py) cover the runnable example. Avoid assuming a preview represents a recorded exposure.

## Configuration reference

All keys below live under `pyfly.feature-flags`:

| Key | Default | Purpose |
|---|---|---|
| `enabled` | `false` | Register the subsystem |
| `openfeature.domain` | `""` | Use the default provider; set a domain to isolate applications sharing a process |
| `flags`, `evaluators` | empty | Configuration definitions and shared rules |
| `sources.file.enabled`, `.path`, `.refresh-interval` | disabled | Watched JSON/YAML document |
| `sources.http.enabled`, `.url`, `.token`, `.timeout`, `.refresh-interval` | disabled | Remote sync document |
| `sources.store.enabled`, `.driver`, `.datasource`, `.refresh-interval` | disabled | Memory or database writable layer |
| `context.tenant-attribute`, `.trust-tenant-header` | `tenant`, `false` | Tenant context source |
| `web.disabled-status` | `404` | Closed route HTTP status |
| `events.evaluations` | `false` | Opt in to exposure events |
| `management.writes` | `false` | Permit store mutations |
| `server.enabled`, `.path`, `.token`, `.allow-anonymous` | disabled, `/feature-flags/flagd.json`, empty, `false` | Sync endpoint and access |

Polling durations must be positive (for example `500ms`, `5s`, `1m`). The [contract](feature-flags-contract.md) governs portable definitions; property binding reports the full invalid key at startup.

## Troubleshooting

| Symptom | Check |
|---|---|
| A gate stays closed | The caller default, definition state, matching variant and source status |
| Every user gets the rollout default | A stable `targetingKey` is present |
| A changed file is ignored | Replace it atomically and check file-source health |
| Another process has old values | Its store/HTTP polling interval and last-good status |
| A write reports `conflict` | Re-read its version; retry a whole aborted caller transaction |
| A put returns `refreshPending` | Commit the outer transaction, then poll GET |
| Exposures are absent | Enable `events.evaluations`, avoid preview, and check the 10,000-occurrence cap |
| An expired flag still evaluates | `expires` tracks debt; disable or delete it explicitly |
