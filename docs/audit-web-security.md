# HTTP dispatch and security audit recording

Native HTTP/security hooks use the same opt-in `AuditRecorder`, per-application DI binder and host-authoritative `AuditContextResolver` as other audit modules. They do not replace authentication, permissions or `HttpSecurity` rules. The [journal](audit.md) stores accepted facts; optional [archive adapters](audit-archive.md) deliver committed facts separately.

## Explicit enrollment and host authority

Enable the journal and recording on the configured native SQL datasource, and supply the trusted resolver bean:

```yaml
pyfly:
  audit:
    enabled: true
    recording:
      enabled: true
      modules: [web, security]
```

The host resolver supplies the exact actor, tenant/workspace/project scope, resource, revision, references and correlation. It must obtain identity from verified server authority. Invalid or missing credentials need an explicit host policy, such as a service actor representing the security decision boundary; never copy an unverified subject or invent an authenticated actor. Client request/transaction ID headers are not authoritative audit correlation. Supply a server-owned operation ID when related request observations must share one correlation.

Hooks pass only bounded, static operation hints. They do not copy tokens, credentials, session IDs, request bodies, headers, raw URLs/query strings, permission expressions, exception text or response bodies into journal facts. Resolving project/resource identity from request context remains a trusted host responsibility; there is no raw-URL or guessed-actor fallback. Review other application access logs and telemetry separately: these hooks do not redefine their privacy policies.

`WebAutoConfiguration` supplies `AuditWebFilter` when recording is enabled. It runs after native request context/authentication filters and before HTTP authorization and handlers. Native DI post-processing enrolls the existing security filter beans lazily into their own application's recorder. Disabled/unconfigured modules preserve existing behavior. A manually constructed filter outside DI is not automatically enrolled; register it as an application bean or record custom semantics explicitly through the public recorder API.

Missing mandatory recorder/context or journal-write failure prevents downstream invocation. Independent observations refuse every held active/completing Data unit, including suspended, read-auto and other-datasource units. Dispatch after the business unit exits; business state mutation and its transactional fact belong to the Data recording seam.

## Exact automatic coverage

| Native adapter | Recorded operation and boundary | Limits |
| --- | --- | --- |
| `AuditWebFilter` in the Starlette/FastAPI filter chain | `web.request`: `started` before downstream dispatch, `observed` after it returns, `unknown` when an exception/cancellation interrupts it | Dispatch observations, not business success, HTTP delivery receipts or exactly-once admission |
| JWT `SecurityFilter` | `security.authenticate`: `admitted` for an established verified context, `denied` for presented credentials that fail authentication | No event inventing a failed authentication when credentials are absent; configured exclude patterns still apply |
| `OAuth2ResourceServerFilter` | `security.authenticate` after native bearer validation; a rejected present token records `denied` before the existing optional401 response | Existing validation/sender constraints and anonymous-versus401 mode remain authoritative; no new token grants |
| `OAuth2SessionSecurityFilter` | `security.session_restore`: `admitted` after restoring an already-authenticated server session context | No invented denial for absent sessions; session creation/login/logout/revocation are not captured by this hook |
| `HttpSecurityFilter` | `security.authorize`: `admitted` before invoking downstream or `denied` before its401/403 response | Existing first-match/deny-by-default rules and permissions remain unchanged; an empty rule set is a no-op, not an invented authorization grant |

Security hints identify the static authentication mechanism or HTTP rule set, not client paths, subjects or raw rule values. The host resolver chooses the approved opaque resource identity. Authentication observations describe the mechanism at that point in the chain; a later configured authentication filter can establish the final request context according to the existing order.

`AuditModulePostProcessor.coverage()` returns a frozen snapshot of configured modules and actual DI-bound adapter classes. Configuration alone does not prove a request passed through an adapter. These hooks do not claim automatic coverage for arbitrary custom providers, method-security decorators, custom ASGI stacks bypassing the native chain, other servers, WebSockets, manual session creation/logout or all business effects. Those paths require explicit integration and qualification.

## Prepared responses, streaming and uncertainty

The filter neither buffers nor inspects stream bodies. Native streaming frames retain their existing order and status. The chain's streaming sentinel is not an actual response-status measurement, so the hook never uses its200 value to infer success. A buffered response can be prepared before bytes are sent; an ASGI send can complete before a remote client receives anything. `observed` means downstream dispatch returned, not that a stream was delivered or a domain operation succeeded.

Exceptions/cancellation attempt to record `unknown` because effects may already have occurred. The native shielded completion helper drains the cancellation settlement attempt despite repeated asyncio/anyio cancellation. Original cancellation is preserved without its message even if storing `unknown` fails; a remaining `started` fact does not establish a retained outcome. Other terminal audit failures propagate. Recording cannot undo transmitted bytes or committed external effects and never authorizes automatic request replay. Keep business effect admission/settlement and safe reconciliation on the appropriate semantic recorder/outbox boundary.

This integration adds journal facts and enrollment coverage, not new HTTP audit metrics, a delivery dashboard or archive/WORM acceptance. Qualify native SQL transactions, host identity resolution, privacy and each selected transport before deployment.
