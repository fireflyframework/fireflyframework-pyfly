# Semantic audit recording

`AuditRecorder` adds mandatory application and domain facts to the [native audit journal](audit.md). Recording is opt-in. A journal bean, a trace, an authenticated request, or an installed module does not establish automatic coverage of every operation.

## Configuration and trusted context

```yaml
pyfly:
  audit:
    enabled: true
    datasource: primary
    recording:
      enabled: true
      modules: [data, eda, agentic]
```

`AuditRecordingAutoConfiguration` is the `audit-recording` entry point. Automatic enrollment requires a nonempty list containing only `data`, `web`, `security`, `eda`, or `agentic`. Unknown names fail startup. The journal schema and mutation guards must already satisfy the configured native schema strategy. There is no in-memory fallback.

The host must provide exactly one selected `AuditContextResolver` bean. Missing or ambiguous dependencies retain native DI errors; one explicitly primary resolver can resolve ambiguity. The recorder uses the same context-owned datasource and transaction manager as the journal. Its lazy postprocessor binding permits a resolver to depend on ordinary application services without constructing a second global manager.

The public neutral contracts are:

```python
from pyfly.audit import AuditContext, AuditContextResolver, AuditOperation

class HostAuditResolver:
    async def resolve(self, operation: AuditOperation) -> AuditContext:
        # Resolve from the host's verified identity and authorized project state.
        return await verified_project_context.audit_facts(operation)
```

`verified_project_context` is an application service in this example, not a framework default. Register the implementation through an `@bean` method returning `AuditContextResolver`. `AuditContext` requires an explicit scope, actor and primary resource. It may include bounded correlation, reference, reason-code and usage facts. The framework does not guess a tenant, project, actor or resource from a request.

`SecurityContextHolder.get_context()` is the native source for security context established by authentication filters or a verified task context. A host resolver can map that identity to an approved opaque subject and authorized scope. Do not copy an email address, URL, token claim set, caller-supplied request ID or body field into audit identity. Workers must derive authority from their admitted server-owned command or job context. An anonymous or invalid-token lifecycle observation needs an explicit host policy; it has no fabricated system actor fallback.

`AuditOperation.resource_kind` and `resource_key` are resolver hints. Native adapters supply static code/configuration identities or native generated delivery identities. The hints themselves are not automatically serialized as facts; the host supplies the exact approved resource. Never derive a hint from SQL, entity values, tool arguments, prompts, secrets, HTTP URLs or payloads.

## Three transaction boundaries

| Method | Boundary and guarantee |
| --- | --- |
| `await recorder.state(operation, outcome="admitted")` | Joins the existing active write unit of the exact native SQL manager. Business state, fact, head and native outbox commit or roll back together. A returned receipt remains provisional until commit. |
| `await recorder.observation(operation, outcome="observed")` | Opens an independent native unit only when this task holds no active Data unit. Suitable for independently durable lifecycle facts; it does not assert atomic business capture. |
| `async with recorder.effect(operation, admission_key=command_key)` | Resolves trusted context once and commits intent before yielding to external I/O. Retains `observed` or `unknown` using the same actor, scope, primary resource and operation correlation. |

State capture rejects a missing, read-only, autocommit or foreign-manager unit. Context, validation and storage failures mark the actual active business unit rollback-only, even when the caller catches the error. Complete mandatory state capture while that unit is active, before its completion decision; best-effort `before_completion`, `after_commit`, event listeners and trace callbacks are not WAL admission hooks. SQLite/PostgreSQL are supported. A MongoDB transaction cannot acquire SQL atomicity by also writing a SQL journal: no such dual-write adapter is provided.

Independent observations and effects reject **all** active or completing held Data units, including read auto-units, another datasource, and a writer suspended by `NOT_SUPPORTED`. Dispatch after the business unit exits, or use the native transactional outbox. Do not work around the boundary with `REQUIRES_NEW` or a detached task retaining a caller's writer locks.

`admission_key` is a bounded server-owned command identity, not a request idempotency header. The recorder derives a scoped intent ID and requires the optional `AuditAdmissionJournal.admit_once` capability. Native SQL admission rejects any existing ID, including an identical prior event, without scanning history. Ordinary `append` remains idempotent and is not an admission gate. Different attempts, claim leases or processes must retain the same authorized scope and command identity. An unsupported admission adapter refuses before I/O.

After the effect boundary yields, an exception means the effect may have occurred. The recorder attempts to retain `unknown`, then raises static `AuditEffectUncertain` with `retryable=False`; it does not return the provider exception or `ModelRetry`. Failure to retain a successful outcome is also uncertain. Cancellation remains cancellation after shielded, awaited cleanup. If storage is unavailable, the already committed intent remains unresolved. There is no automatic refund, replay or claim of exactly-once delivery.

The effect handle can enrich only typed usage and resource references. It cannot replace the frozen actor, scope, primary resource or admitted operation. `observed` records completion of the invocation boundary, not business success, remote durable commit, browser receipt or an invoice. Missing monetary usage remains missing.

## Automatic, manual and unsupported coverage

Automatic means the corresponding native adapter is actually enrolled in that application. Other entry points remain manual unless explicitly listed below.

| Module | Automatically enrolled boundary | Manual or unsupported boundary |
| --- | --- | --- |
| Data SQL | Managed repository write admissions inside their actual native unit, including compiled modifying methods and nested managed writes. Reads do not generate write facts. | Raw SQL, direct sessions and custom transactional methods executing direct SQL require explicit `state`. An enrolled repository with a separately supplied manual session refuses writes. Entity `created_by`/`modified_by` fields are mutable metadata, not journal/WAL capture. |
| EDA | Native SQL outbox enqueue in the same unit; owned transactional/database publisher store/relay composition. Delivery admission commits before a subscription handler. Stable native outbox/group/subscription identity fences repeated delivery after unknown, crash, lost acknowledgement or settlement failure. | Direct non-outbox brokers and Mongo outboxes are not automatic SQL capture. Strict audited relay refuses unsupported stores, foreign managers and external dead-letter stores before claiming. Reconciliation is explicit; an observed handler is not proof of downstream business commit. |
| Agentic | Runtime-bound native agent runs; supported model request/stream attempts; native `AgentTool` after argument validation and native guards/listeners; acyclic pipeline run/node dispatch after preparation. | Standalone unbound agents/models, raw SDK functions/`Tool`/toolsets, opaque provider/wrapper models, cyclic/dynamic pipelines and checkpoint resume require a reviewed host adapter or explicit semantic integration. Mandatory automatic mode rejects unsupported configured tools/models before admission. Adapt functions through native `AgentTool` rather than silently relying on agent-level facts. |
| Web/security | Native lifecycle and authorization adapters require separate explicit enrollment; consult their adapter coverage when configuring them. | No blanket coverage of custom middleware, alternate authentication mechanisms, programmatic method security, all routes or all denials follows from enabling the journal. Business state still uses its transaction boundary. |
| Client, MCP/server, WebSocket | Manual semantic recorder capability. | No automatic coverage of HTTP/connector invocation, MCP tool dispatch or transport sessions in this slice. |
| Credentials, secrets, IDP/OAuth2 | Manual approved opaque reference/version facts. | No implicit credential-access capture, key/token contents, claims or provider endpoint recording. |
| Scheduler/executors, callbacks, webhooks, notifications, ECM | Manual task admission, dispatch and outcome boundaries. | No automatic vendor delivery, task authority inference or external storage/signature coverage. |
| CQRS, event sourcing, sagas/workflows, domain | Manual semantic state and dispatch facts on their actual native units. | Module presence does not establish aggregate, saga or event-store capture. |
| Container/context/AOP/session, actuator/admin, cache, config/config-server, feature flags, plugins, resources, rule engine, resilience, logging/observability, CLI/shell/starters/testing/validation/core/i18n/kernel | Composition infrastructure or explicit host producers. | No automatic business history from bean creation, traces, logs, cache operations or entity auditing. |

The automatic Agentic adapter uses the async interface on the host application's owning event loop. Mandatory mode refuses `run_sync` and foreign-loop execution before I/O; the default disabled mode retains existing sync/loop behavior. Its model adapter currently supports OpenAI Chat/Responses classes and offline `TestModel`/`FunctionModel` fixtures. It forces request-local SDK `max_retries=0`, preserving the borrowed client configuration. Each native request/continuation and stream has its own awaited effect boundary. Other providers or retrying model wrappers are refused until a per-attempt adapter is qualified. Offline fixture tests are not live-provider acceptance. Tool decorators and pipeline/rate-limit retries preserve non-retryable uncertainty. No token counts are treated as USD, and no monetary zero is invented when usage is unavailable.

Native PydanticAI run IDs provide stable agent-command admission keys. If a host accepts an explicit run ID, it must authorize its scope and command ownership before passing it to the agent; a client-provided ID is not authority. Repeated admission of the same scoped native run ID is refused, including after an observed run. Invocation-local uncertainty remains fenced through SDK recovery and native resilience wrappers.

After committed intent, the recorder's private execution fence prevents native generic `@retry` from repeating a function inside the effect body, including a decorator inside an `AgentTool` handler. The fence is task-local, inherited by children and reset on exit; it does not suppress another application's state facts. Native terminal exceptions with a `retryable=False` property still refuse retry outside that region. This does not control arbitrary host loops or third-party SDK retries inside a handler: disable those retries or provide a reviewed per-attempt adapter.

The supported custom `AgentTool` extension is `_execute`. Automatic mode validates the current native execution/guarded/listener methods and known handler mapping before any model I/O. Overriding or replacing public execution methods, the guarded chain or the handler adapter is unsupported and refused; some cached/composed wrappers therefore need an explicit host adapter. Native decorated tools and ordinary `_execute` subclasses remain supported. Shared tool objects are not rebound to application-specific recorders.

An acyclic pipeline command identity is fenced conservatively after admission, including after an observed run. Reusing it is not permission to rerun effects. Cyclic/dynamic visit identities and checkpoint reconciliation require a separate reviewed adapter. A direct host can deliberately issue a new authorized command; automatic retry cannot convert an unresolved command into one.

## Inspect enrollment without overstating it

`AuditModulePostProcessor.coverage()` returns a new frozen `AuditRecordingCoverage` snapshot:

- `configured_modules`: explicit automatic enrollment configuration;
- `di_bound_adapters`: actual DI-bound adapter type identifiers and their declared modules.

An empty adapter list does not mean capture occurred. The report covers DI bindings, not every instance or operation. Owned nested adapters and shared `AgentTool` objects reached through an enrolled runtime are described by their composition; they are not falsely listed as separately bound beans. Deferred request/session proxies are not materialized at singleton startup. A shared adapter instance cannot be rebound to another application. Custom domain modules can use a manually constructed recorder with explicit module identifiers; that is separate from automatic enrollment.

## Telemetry and application adoption

The native journal borrows DI tracer/meter providers and keeps telemetry best effort. Spans and metrics are not the mandatory journal. Correlation must come from trusted host context; no exporter, trace failure or request label can waive admission. The private infrastructure marker is scoped to the actual journal owner, including native proxy normalization. It suppresses only recursive infrastructure Data/EDA capture, not another application's journal or arbitrary security/Agentic/manual facts. Journal outbox/archive work cannot opt out using an event-name prefix or request flag.

An application already appending authoritative semantic facts directly to `AuditJournal` can keep that contract without configuring recording. To adopt automatic hooks, add a verified host resolver, enroll only intended modules and review semantic overlap. Keep an application's reservation/settlement or governance facts authoritative; generic invocation observations do not replace those ledgers or authorize duplicate pricing/effect facts. There is no backfill pretending that historical rows were captured before enrollment.
