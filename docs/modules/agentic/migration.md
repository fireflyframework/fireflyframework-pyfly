# Migration Guide

Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0.

Start with the native PyFly migration below. Later sections preserve the standalone
26.10.1 dependency refresh, PostgreSQL vector-store safety in 26.10.0, the
Pydantic AI 2.x upgrade and earlier API changes as dated source history.

## Move from standalone Agentic to PyFly

Native Agentic is an opt-in part of the `pyfly` distribution. This is a namespace
break: there is no compatibility package or import hook for the old namespace.
The sections after this migration checklist preserve earlier standalone behavior
changes; their release numbers do not name a PyFly Agentic release.

| Concern | Standalone | Native PyFly |
| --- | --- | --- |
| Distribution | `fireflyframework-agentic` | `pyfly` with `agentic` extras |
| Import prefix | `fireflyframework_agentic` | `pyfly.agentic` |
| Optional extra | `postgres`, `binary`, etc. | `agentic-postgres`, `agentic-binary`, etc. |
| Environment | `FIREFLY_AGENTIC_*` | `PYFLY_AGENTIC_*` |
| Configuration | Standalone settings | `pyfly.agentic` plus typed `AgenticConfig` |
| Activation | Import/use standalone library | `pyfly.agentic.enabled: true` in a PyFly application |
| Agent plugin group | `fireflyframework_agentic.agents` | `pyfly.agentic.agents` |
| Tool plugin group | `fireflyframework_agentic.tools` | `pyfly.agentic.tools` |
| Reasoning plugin group | `fireflyframework_agentic.reasoning_patterns` | `pyfly.agentic.reasoning_patterns` |
| Logger / instrumentation identity | Former package namespace | `pyfly.agentic` namespace; update selectors and dashboards |
| Version lookup | `version("fireflyframework-agentic")` | `version("pyfly")`; package version follows PyFly |
| State ownership | Process defaults | Application-owned `AgenticRuntime` |

1. Use a verified PyFly release, v26.10.08 or later, with its `agentic` extras following [installation](getting-started/installation.md).
   Remove the standalone distribution from the application's dependency manifest
   and resolve a clean environment. Do not assume an older published PyFly wheel
   includes Agentic; verify the selected release and its artifacts.
2. Replace imports, patch targets, plugin metadata, dotted class paths and serialized
   Python-qualified names. Rename public objects and decorators using the table
   below. The native module does not expose aliases for the old names.
3. Prefix Agentic integration extras with `agentic-`. Existing PyFly `security`,
   `postgres`, `dev` and `full` extras keep their original meanings. Update install,
   uninstall and container-build automation to install PyFly rather than the old repository.
4. Move configuration under `pyfly.agentic`, enable it explicitly, rename environment
   keys and preserve field underscores (for example `PYFLY_AGENTIC_DEFAULT_MODEL`).
   Provider credential keys remain provider-owned. Review explicit nullable fields
   and dictionary overrides in the final deployment environment.
5. Inject `AgentFactory`, `AgenticRuntime`, `AgentRegistry`, `MemoryManager` or
   `UsageTracker` through PyFly. Create agents with `factory.create(name, **kwargs)`.
   For an embedded script, use `async with AgenticRuntime(config=...) as runtime`
   and `with runtime.activate():`. Run import-time registration and plugin discovery
   in the intended activation scope. See the [complete native tutorial](native-runtime.md).
   An agent exposed through `@bean` must come from that factory or receive the
   application's explicit `runtime=`; startup rejects unmanaged or foreign agents.
6. Let the application context close its runtime. Manually owned runtimes use
   `await runtime.aclose()`. Finish stream contexts on early exits. Do not close
   injected services per request or pass one application's registry/ledger into another.
7. Update log filters, telemetry library selectors, dashboard queries and provider
   ownership. Native telemetry attributes and metrics use `pyfly.agentic.*`;
   migrate custom `firefly.*` queries to the documented native schema. Configure exporters in the host, without installing
   a second process-global provider from application services.
8. Re-run offline contracts, application startup/shutdown, tenant-scope checks,
   actual persistence restore and the selected provider API tests. Track downstream
   consumers separately; publishing a replacement does not migrate Studio, Forge,
   or another application automatically.

### Public object and decorator names

| Standalone name | Native name |
| --- | --- |
| `FireflyAgent` | `Agent` |
| `BaseTool` | `AgentTool` |
| `FireflyAgenticConfig` | `AgenticConfig` |
| `FireflyAgenticError` | `AgenticError` |
| `FireflyMemoryError` | `AgenticMemoryError` |
| `FireflyAgentRunner` | `ManagedAgentRunner` |
| `FireflyTracer` | `AgentTracer` |
| `FireflyMetrics` | `AgentMetrics` |
| `FireflyEvent` / `FireflyEvents` | `AgentEvent` / `AgentEvents` |
| `@firefly_agent` / `@firefly_tool` | `@agent` / `@agent_tool` |

`AgentRunner` remains the workflow protocol; `ManagedAgentRunner` is its native
managed implementation. Imports, type annotations, decorators, subclass bases,
configuration class paths and mocks must all use the native names. Organization,
license and source-attribution names remain Firefly where legally appropriate.

`AgenticConfig` is a typed Pydantic model, not an independent Pydantic Settings
loader. Direct construction accepts explicit values only. Native applications use
PyFly's effective configuration tree; an embedded script can call
`bind_agentic_config(Config())`. There is no implicit `.env` loading. An application
that elects to use a dotenv file must load it explicitly before constructing its
configuration tree.

Telemetry attributes and metric names now use `pyfly.agentic.*`; update queries,
filters and dashboards previously reading `firefly.*` as part of cutover. Existing
storage paths/schema names remain data locations; they are not import aliases.

### Data and operational compatibility

The native data cutover changes memory identity constraints and resource ownership
as detailed below. Vector dimensions, checkpoint meaning and provider endpoints
retain their documented contracts. It does not automatically migrate files,
databases, conversations or journals. Preserve configured paths and namespaces and
exercise backup/restore with disposable data before cutover. Python pickles and custom
payloads containing qualified class names may require an application-specific conversion;
never infer compatibility solely from unchanged JSON field names.

Conversation history is process-local unless explicitly exported/imported. Persistent
working memory does not by itself persist full conversation history. Pgvector document
IDs remain globally unique within the vector table. Native memory instead uses
compound namespace/entry identity. The
native runtime isolates state between applications, while authentication, database RLS,
tool side-effect idempotency and provider-side spending limits remain application concerns.

### Native Data and runtime cutover

1. Back up existing working facts and preserve the actual location:
   `firefly_memory.memory_entries` for PostgreSQL and `firefly_memory.entries`
   for Mongo unless explicitly configured otherwise. PostgreSQL's `content`
   column retains the complete `MemoryEntry` JSON; do not unwrap/re-encode it.
   Mongo retains top-level entry fields and BSON dates.
2. Provision native PyFly Data relational/document configuration. Move old
   Agentic connection URLs, pool sizing and driver options there. Select the
   intended relational `memory_datasource`; Mongo borrows the matching native
   async PyMongo client and document transaction manager. Independent asyncpg
   pools and Motor clients are no longer Agentic-owned resources.
3. Migrate PostgreSQL memory identity to `PRIMARY KEY (namespace, entry_id)`,
   removing global `entry_id` uniqueness and provisioning lookup/expiry indexes.
   In Mongo replace global unique `entry_id` indexing with unique
   `(namespace, entry_id)` plus lookup/order and expiry indexes. Validate data and
   constraints with your migration system before cutover. Startup rejects old
   global identity without changing rows/documents or dropping indexes.
4. Install `agentic-relational`, `agentic-postgres`, `agentic-mongodb` or
   `agentic-vectorstores-pgvector` as needed. They compose native Data dependencies;
   Mongo uses async PyMongo rather than Motor. Native relational memory follows
   Data `ddl-auto` and explicit `memory_create_table`; production `none`/`validate`
   requires migrations. Mongo `memory_create_indexes=False` validates existing
   indexes. These choices do not silently turn on persistence.
5. Replace `PostgreSQLStore(url, ...)` with `PostgreSQLStore(datasource, ...)`,
   `MongoDBStore(url, ...)` with `MongoDBStore(client=..., manager=..., ...)`, and
   `PgVectorVectorStore(url, ...)` with `PgVectorVectorStore(datasource, ...)`.
   Removed URL/pool arguments raise migration errors. Initialize adapters on the
   application loop before business transactions; finish Agentic before Data
   shutdown. Closing an adapter never disposes its borrowed pool/client.
6. Await `MemoryManager.async_set_fact`, `async_get_fact`,
   `async_get_working_context` and `WorkingMemory.async_*` in async code. Native
   memory and pgvector join the caller's unit of work and refuse read-only writes.
   Sync memory may bridge from another thread to the running application loop
   with ambient context; sync calls on that owner loop fail promptly. Do not use
   `to_thread` to initialize a second Data loop. Atomic working-key replacement
   requires a transactional unit and a transaction-capable Mongo deployment.
7. Review pgvector `_prepare_session`/`_create_schema` subclasses. Their connection
   argument is a guarded SQLAlchemy-backed facade with `execute`, `fetchval`,
   `fetch` and `$1` binds; `execute` returns a SQLAlchemy result. There is no raw
   driver/pool/commit surface. Transaction-local RLS settings remain supported;
   pgvector IDs stay globally unique across its table. Follow its explicit
   `create_schema` / native `ddl-auto` policy and test with the application role.
8. Enable content encryption through `encryption_enabled` plus key or injected
   `EncryptionProvider`. Auto-composed memory preserves typed JSON and wrong-key
   reads fail closed. Metadata, namespaces, keys, timestamps and conversations
   are outside this encryption coverage. A custom `MemoryManager` replaces the
   whole composition/confidentiality policy and must supply its own protection.
9. Replace generic HTTP pools with injected `HttpClientPort`/`pyfly.client`
   settings. Keep provider SDKs as specialized adapters. Use awaited AgentEvents
   methods for native listeners; explicitly configure both EDA publisher and
   destination if exporting. Exported plugin classes/factories now use Container
   injection and managed lifecycle; registered beans remain borrowed. Agent
   factories must return an Agent bound to the injected factory/runtime.
10. Remove inert controls using the complete
    [configuration migration map](configuration.md#removed-controls-and-native-replacements).
    Move `FIREFLY_DBSTORE_CACHE_ROOT` to `PYFLY_AGENTIC_ARTIFACT_CACHE_ROOT`, keeping
    the same path. Requalify commit/rollback, restore, scope isolation, read-only
    refusal, async/sync bridges, startup/shutdown and injected resource ownership.

Conversations remain process-local unless explicitly exported/imported; memory
persistence is not transcript persistence. Artifact storage, file journals and
provider/vector-specific protocols keep their specialized semantics. No generic
native repository adapter or exactly-once tool execution is implied.

### Lightweight file and SQLite identity cutover

File and stdlib SQLite backends also require explicit legacy identity review.
FileStore keeps safe filenames and its entry-ID-to-entry JSON payload. Names
containing separators or starting `__pyfly_namespace_v1__` use that prefix plus
the exact namespace's UTF-8 hex bytes and `.json`. Adjacent `<filename>.namespace`
markers contain a JSON string identifying the original namespace and remain
after clear. Existing unmarked underscore filenames are ambiguous and all CRUD
fails closed until ownership is confirmed; never infer ownership from the old
lossy basename. Preserve payload bytes, explicitly move confirmed unsafe names
to encoded basenames, provision exact-namespace markers, and keep unmarked
backups outside the active directory. Back up markers together with data.
See the [exact file examples](memory.md#file-namespace-identity-and-legacy-migration).

`SQLiteStore` preserves its supplied file, `firefly_memory` table and full-entry
JSON. Global entry-ID primary/unique constraints are refused before modification.
A reviewed migration must copy rows unchanged and in original rowid order into
a compound `(namespace, entry_id)` schema, verify them, preserve a backup and
provision the active namespace index. A retained old table may own the historic
index name; use an available name on the new active table. No automatic
constraint drop, table rebuild or data rewrite occurs. See
[SQLite migration details](memory.md#sqlite-identity-and-migration).

### Earlier standalone changes

The remaining sections are historical API and dependency guidance preserved from
the source guides, with code imports adapted to `pyfly.agentic`. Read references to
process defaults or old version numbers in that historical context; native ownership
is defined by [AgenticRuntime](native-runtime.md).

## Dependency refresh in 26.10.1

Resolve application dependencies normally after upgrading the release wheel.
The Pydantic AI minimum is now `>=2.53.0,<3`; the qualified lock uses 2.55.0 with
FastMCP 4.1.0, MCP 2.3.0, OpenAI 3.31.0 and Starlette 1.7.0. Existing provider
extras, native toolsets, model wrappers, output schemas and endpoint selectors
remain available. Do not bypass dependency resolution or retain incompatible
application overrides. The [resolution record](dependency-upgrade-26101.md)
lists all 23 Dependabot requests and the remaining optional-package advisories.

Applications using native Pydantic AI concurrency limiters must account for its
2.53 changes: a limiter cannot be shared between a calling agent and nested
model wrappers, acquisition is no longer reentrant, and custom limiters must
support release from another task. Pydantic AI 2.55 also accounts for usage from
rejected fallback attempts. Review cost reporting and custom wrappers when
upgrading; applications requiring that accounting correction need Pydantic AI
2.55 or later even though Agentic's minimum is 2.53. Firefly records aggregate
native fallback tokens, but does not attribute their dollar cost separately to
each attempted provider/model. Incremental streams now close unfinished native
text iterators before leaving the stream context, preventing deferred cleanup
errors after early termination. Firefly's sampling validation follows the new
`thinking_enabled_by_default` profile key; custom measured profiles should use
that key instead of `openai_reasoning_enabled_by_default`.

The `[security]` extra now requires cryptography 50 or later. Upstream stopped
publishing macOS Intel and Windows 32-bit wheels in version 49. A universal
Agentic wheel does not establish native dependency support on those platforms;
qualify a supported platform or an independently maintained source build.

The `[binary]` extra retains HEIF support and updates Pillow, pypdf, CairoSVG and
py7zr. The 7z adapter uses the current in-memory writer API and enforces member
and expanded-size limits before returning extracted artifacts. No database
migration, re-embedding or PostgreSQL upgrade is required.

The Mistral embedding adapter now supports the SDK 2 client module while
retaining the 1.x import path. The qualified lock uses Mistral 2.10.1; local HTTP
tests exercise construction, request serialization, response parsing and errors.

## PostgreSQL vector-store safety in 26.10.0

Previously, upserting an existing document ID into another namespace could
overwrite and move the original PostgreSQL row. Version 26.10.0 raises
`VectorStoreError` for that conflict and rolls back the whole batch, including
earlier inserts or updates. The check also applies to concurrent writes and
`TenantScopedVectorStore`.

The table still uses a global `PRIMARY KEY (id)`. Same-namespace updates and
repeated IDs within one same-namespace batch keep their existing behavior. No
database migration, index rebuild, or re-embedding is required for this fix.

Before upgrading a shared-table deployment, check how the application assigns
document IDs. IDs must be unique across namespaces. New documents in different
scopes need distinct IDs; do not bulk-rename existing IDs without accounting for
their references. If the application used cross-namespace upsert to move a
document, replace that behavior with an explicitly authorized transfer workflow.
Retrying the same conflicting batch will not succeed.

Install the [current release](getting-started/installation.md#from-source),
then verify same-namespace updates, rejection of a foreign-namespace ID, and
whole-batch rollback in an isolated application test database. This fix does not
restore content overwritten by earlier versions; any suspected historical loss
requires review of application audit records or backups.

The wrapper requires scope arguments but does not authenticate them or install
RLS policies. Keep authorization and transaction-local database scope setup in
the application. See [vector-store scoping](vectorstores.md#tenantworkspace-scoping)
and [database authorization](security.md#vector-store-authorization).

Pgvector/HNSW remains unchanged. PostgreSQL 18 and pg_vectorsearch are not
requirements of this release; [PRISM remains outside the supported backend](pg-vectorsearch-evaluation.md).

## Pydantic AI 2 and OpenAI API selection

The framework now requires `pydantic-ai>=2.53.0,<3` and
`genai-prices>=0.1.9,<0.2`. Update application constraints that still require
Pydantic AI 1.x, then resolve dependencies and run your application tests. The
framework declares provider extras explicitly to preserve its existing provider
integrations under the new dependency layout.

### Existing OpenAI agents keep Chat Completions

The framework preserves the endpoint for existing configurations:

```python
# Before and after the dependency upgrade: Chat Completions.
agent = Agent(name="assistant", model="openai:gpt-4o")
spec = ModelSpec(provider="openai", model="gpt-4o")

# Explicit equivalents for new/shared configuration.
agent = Agent(name="assistant", model="openai-chat:gpt-4o")
spec = ModelSpec(provider="openai-chat", model="gpt-4o")
```

The same rule applies to `azure:` / `provider="azure"`, with `azure-chat:` /
`provider="azure-chat"` as the explicit form. An existing preconfigured
`OpenAIChatModel` stays on Chat Completions; an `OpenAIResponsesModel` stays on
Responses. `ModelSpec(model_class="OpenAIResponsesModel", provider="openai", ...)`
remains valid.

Explicit API selection also applies to fallback models and evaluation judges.
After a fallback run, Firefly restores the original model and identifier and
resets the fallback chain, including when the run succeeds or fails.

If application code constructs a **bare `pydantic_ai.Agent`**, its `openai:` prefix
now selects Responses. Use `openai-chat:` there to retain the old endpoint, or
`openai-responses:` to adopt the new one deliberately. This also matters when
sharing a model string with an upstream library outside Firefly. See
[Pydantic AI's OpenAI configuration](https://pydantic.dev/docs/ai/models/openai/).

### Opt into Responses

```python
from pyfly.agentic.agents import Agent
from pyfly.agentic.models import ModelOptions

# Reads OPENAI_API_KEY from the environment.
agent = Agent(
    name="assistant",
    model="openai-responses:gpt-6-luna",
    model_options=ModelOptions(reasoning="low", store_responses=False),
)
first = await agent.run("The project name is Lantern. Acknowledge it.")
second = await agent.run("What is its name?", message_history=first.all_messages())
```

Your tool definitions, output models, and `run` / `run_sync` / `run_stream` calls
keep the same interface. Pydantic AI handles each endpoint's request and response
shapes. Use `provider="openai-responses"` in `ModelSpec`, or `azure-responses` for
an Azure deployment that supports Responses. Custom OpenAI-compatible services
must implement the selected API; changing a prefix cannot add server support.

For GPT-6 model and endpoint restrictions, follow the
[compatibility table](models.md#gpt-6-model-selection). In particular, migrating
an Astra agent with tools requires Responses. Review reasoning effort and remove
sampling/log-probability settings that the selected mode rejects, including any
configured default temperature. See the
[official OpenAI migration guide](https://developers.openai.com/api/docs/guides/latest-model/gpt-6-astra.md#migration-quickstart).

### Settings, profiles, and conversation history

- Prefer `ModelOptions` for framework-owned settings, including reasoning and
  provider response storage. Pass `model_options=` to an agent or individual run,
  or `options=` to `ModelSpec`. Unsupported options raise `ModelOptionsError`.
  See the [typed option reference](models.md#typed-model-options).
- `ModelSpec.settings` preserves native Pydantic AI provider settings such as
  `openai_reasoning_effort` and `openai_store`, alongside the translated console
  settings. Pass `factory.settings_for(spec)` to `Agent(model_settings=...)`.
- Pydantic AI 2.x model profiles are dictionaries. Replace custom code that uses
  `dataclasses.replace(profile, ...)` or attribute access with dictionary merging
  and key access. `ModelSpec.profile_overrides` remains a mapping.
- `message_history` and Firefly memory continue to carry conversation state.
  Optional `openai_previous_response_id="auto"` chains stored Responses using
  response IDs from that history. It requires storage; leave it unset with
  `openai_store=False`. See the [conversation-state example](agents.md#responses-conversation-state).
- Completed buffered and incremental streams now persist their full messages to
  Firefly memory, including provider metadata and Responses reasoning state.
  Interrupted streams do not persist an incomplete conversation turn.

The offline HTTP compatibility suite checks the SDK/framework request boundary.
It does not establish live model access. Use the credential-gated OpenAI suite for
your selected deployment and model before rollout; commands and scope are in
[tests/README.md](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/tests/agentic/catalogue.md#live-model-tests).

### Database memory lifecycle

PostgreSQL and MongoDB clients now stay on a persistent event loop owned by their
store. This permits initialization and the synchronous memory facade to share the
same database connection safely. Existing `await store.initialize()` and
`await store.close()` remain supported; `initialize_sync()` and `close_sync()` are
available to synchronous hosts. `MemoryManager.from_config()` initializes the
backend, and applications must call `manager.close()` or `await manager.aclose()`
after every manager/fork using the shared backend has finished.

Initialization through `from_config()` and synchronous memory methods block the
caller. Async hosts can offload the factory with `asyncio.to_thread()` and use the
store's async methods for nonblocking I/O. See [Memory](memory.md#shutdown).

### Capabilities and instrumentation

Firefly exposes Pydantic AI 2.x `capabilities=` for native tools and other agent
capabilities. Existing framework tools still use `tools=` / `toolsets=`. See the
[native-capabilities example](agents.md#native-capabilities). Firefly explicitly
keeps `end_strategy="early"`; `"graceful"` and `"exhaustive"` are opt-in.

Native instrumentation defaults to version `5`; legacy configured versions `1`
through `4` map to `5`, and version `6` can be selected explicitly. Pydantic AI 2.x
removed the separate log-event mode: `instrumentation_event_mode` remains accepted
for configuration compatibility but has no effect, and events are represented as
span attributes. Update telemetry queries and exporters that consumed the old log
events. Content inclusion remains controlled by `instrumentation_include_content`.

### Optional evaluation dependencies

The `[evaluation]` extra retains [Ragas 0.2.6](https://pypi.org/project/ragas/0.2.6/)
(`>=0.2.6,<0.2.7`) with `langchain-community>=0.3.31,<0.4`. This is a compatibility
pin: Ragas 0.4.3 requires Instructor, whose
[`jiter<0.15` constraint](https://pypi.org/pypi/instructor/1.17.0/json) conflicts
with [OpenAI 3.19's `jiter>=0.16`](https://pypi.org/pypi/openai/3.19.0/json).
Intermediate Ragas 0.2.7–0.3.1 also patches asyncio during import.

The pinned extra resolves with the upgraded SDK. Framework metric adapters pass
LLM and embedding clients through Ragas `evaluate(...)` so its wrappers apply
correctly. Offline tests exercise the actual metric implementations and check
that importing Ragas preserves the active event loop; this does not claim support
for the latest Ragas release or validate live judge quality.

---

## 1. Tool parameters use a real `python_type` (breaking)

**Why.** Tool parameter schemas were declared with a *string* `type_annotation`
resolved through a tiny built-in map that only understood `str`/`int`/`float`/
`bool`/`list`/`dict` and **dropped** generic element types, enums and nested
models. The LLM therefore saw lossy schemas (`list[str]` became an untyped array,
`Literal[...]` lost its `enum`). `ParameterSpec` now takes a **real Python type
object**, which pydantic-ai introspects directly for a full-fidelity schema.

The string `type_annotation` field and its resolver (`_TYPE_MAP`,
`_resolve_param_type`) have been **removed** — there is one way to declare a type.

### `ParameterSpec`

```python
# Before
ParameterSpec(name="tags", type_annotation="list[str]")          # array with no item type
ParameterSpec(name="body", type_annotation="dict[str, Any] | None")

# After
ParameterSpec(name="tags", python_type=list[str])                # array of strings
ParameterSpec(name="body", python_type=dict[str, Any] | None)
ParameterSpec(name="mode", python_type=Literal["fast", "slow"])  # now a real enum
```

`python_type` defaults to `str`, so `ParameterSpec(name="q")` is a string param.

### `ToolBuilder.parameter()`

The second argument is now the real type, not a string:

```python
# Before
ToolBuilder("weather").parameter("city", "str", description="City name")

# After
ToolBuilder("weather").parameter("city", str, description="City name")
```

### Built-in tools

All built-in tools were migrated; if you subclassed `AgentTool` or built tools with
`ParameterSpec`/`ToolBuilder`, replace each string type with the real type. There is
no compatibility shim — a stray `type_annotation=` keyword is simply ignored by the
model (it is no longer a field).

---

## 2. Tools can opt into `RunContext` (new, additive)

A tool can now receive pydantic-ai's `RunContext` (agent deps, usage, retry count,
messages) by setting `takes_ctx=True`. The context is delivered to `_execute` as the
keyword-only `_ctx`; **guards and the cache never see it**, so it cannot poison a
cache key.

```python
class SetPriority(AgentTool):
    def __init__(self) -> None:
        super().__init__("set_priority", takes_ctx=True,
                         parameters=[ParameterSpec(name="level", python_type=str)])

    async def _execute(self, *, _ctx=None, **kwargs):
        return f"{_ctx.deps}: {kwargs['level']}"   # reach the agent's deps
```

Decorated tools (`@agent_tool`) that declare a `RunContext`-first parameter already
get it natively — no change.

---

## 3. Native toolset combinators are re-exported (new, additive)

`pyfly.agentic.tools` now re-exports pydantic-ai's `RunContext` and the
native toolset combinators (`FilteredToolset`, `PrefixedToolset`, `RenamedToolset`,
`CombinedToolset`, `WrapperToolset`, `PreparedToolset`, `ApprovalRequiredToolset`),
plus `to_pydantic_handler(tool)`. Import them from the tools package instead of
reaching into `pydantic_ai.toolsets`. `ToolKit.as_toolset()` now also forwards each
tool's **description** to the model (it was previously dropped).

---

## 4. `ManagedAgentRunner` is the default workflow runner (behavioural)

**Why.** Workflow sub-agents used to run as a bare `pydantic_ai.Agent`, bypassing the
whole framework. They now run through a `Agent` by default, inheriting the
middleware chain (logging, prompt/output guards, cost guard, caching, observability,
explainability, validation, retry), the 429 rate-limit retry loop, the **global usage
tracker / budget gate**, and model fallback.

What changes for you:

- **Cost is now tracked globally** for workflow sub-agents, and a configured global
  `budget_limit_usd` (BudgetGate) now applies to them — a workflow can raise
  `BudgetExceededError` where it previously would not. Per-run `WorkflowBudget` is
  unchanged and still applies on top.
- The **model-resolution contract is unchanged**: you still pass `model=` per call or
  `default_model=` on the runner; nothing new is required.

To keep the old lightweight, bare-`pydantic_ai.Agent` behaviour, opt out explicitly:

```python
from pyfly.agentic.workflows import DefaultAgentRunner

await my_workflow(args, runner=DefaultAgentRunner())   # the lightweight path
```

---

## 5. Per-call agent targeting with `using=` (new, additive)

A single workflow can route different calls to different configured agents — for
multi-model sub-agents and per-task cost optimisation:

```python
@workflow(name="triage")
async def triage(args, ctx):
    label = await agent("classify the ticket", using="cheap-classifier")
    return await agent("write the resolution", using="premium-writer")

await triage(args, runner=ManagedAgentRunner())   # both resolved from the registry
```

`using` accepts a `Agent` instance or a registry name. With a non-
`ManagedAgentRunner` runner it raises a clear error.

---

## 6. `ApprovalGuard` removed — human-in-the-loop is now native (breaking)

**Why.** Tool approval was a bespoke guard (`ApprovalGuard(callback)`) that ran inside
Firefly's guard chain and raised `ToolError` on a denied call — a synchronous, all-or-nothing
gate with no pause/resume, metadata, or per-call granularity, parallel to pydantic-ai's own
deferred-tools protocol. It has been **removed** in favour of the native protocol
(`requires_approval`, `DeferredToolRequests`/`DeferredToolResults`, `ApprovalRequired`,
`ApprovalRequiredToolset`).

Previously, `ApprovalGuard(callback=approve)` wrapped a tool and blocked denied
calls. Remove that guard and mark the tool `requires_approval=True`. This complete
replacement pauses, asks for an explicit decision, and resumes the same history:

```python
import asyncio

from pyfly.agentic.agents import Agent, is_deferred
from pyfly.agentic.tools import DeferredToolResults, agent_tool

records = {"42": {"title": "Draft invoice"}}

@agent_tool("delete_record", description="Delete a local example record", requires_approval=True)
async def delete_record(record_id: str) -> str:
    removed = records.pop(record_id, None)
    return "Deleted" if removed is not None else "Record not found"

agent = Agent(name="approved-delete", tools=[delete_record])
result = await agent.run("Delete record 42")
if is_deferred(result):
    approvals = {}
    for call in result.output.approvals:
        answer = await asyncio.to_thread(input, f"Approve {call.tool_name} {call.args}? [y/N] ")
        approvals[call.tool_call_id] = answer.strip().lower() == "y"
    result = await agent.run(
        message_history=result.all_messages(),
        deferred_tool_results=DeferredToolResults(approvals=approvals),
    )
```

For the old **inline, non-pausing** behaviour (a callback decides programmatically), pass
`Agent(approval_handler=...)` — wired as a native `HandleDeferredToolCalls` capability.
See [Human-in-the-Loop Tool Approval](tools.md#human-in-the-loop-tool-approval).

Also: guard denials (validation, rate-limit, sandbox) now raise `ToolGuardError` instead of a
plain `ToolError`. `ToolGuardError` **subclasses** `ToolError`, so existing `except ToolError`
handlers keep working.

---

## Checklist

- [ ] Replace every `type_annotation="..."` with `python_type=<real type>` in
      `ParameterSpec` and `ToolBuilder.parameter(name, <type>)`.
- [ ] (Optional) Enrich types now that they are real — `list[str]`, `Literal[...]`,
      nested models — for better LLM tool schemas.
- [ ] (Optional) Add `takes_ctx=True` to tools that need agent deps/usage.
- [ ] Review workflows for global cost/budget effects now that sub-agents run through
      `Agent`; pass `runner=DefaultAgentRunner()` if you want the old path.
- [ ] Import toolset combinators / `RunContext` from `pyfly.agentic.tools`.
- [ ] Replace `ApprovalGuard` with `requires_approval=True` + the native pause/resume flow
      (`is_deferred()` + `deferred_tool_results=`), or an inline `approval_handler=`.
- [ ] If you matched on `ToolError` from guard denials specifically, note it is now the
      `ToolGuardError` subclass (still caught by `except ToolError`).

## Monty interpreter compatibility

The native `agentic-script-execution` extra pins **`pydantic-monty==0.0.18`**, the
interpreter API used by the preserved sandbox implementation. Later Monty 1.x
releases expose a worker-pool constructor and remove the `run_async` interpreter
API; they are not drop-in replacements. Retain the declared pin when resolving
dependencies and qualify any future adapter upgrade with the execution contracts.

## Qualified dependency baseline

Native Agentic pins **Pydantic AI 2.59.0**, matching the source-qualified API and
provider contracts. `agentic-script-execution` pins **pydantic-monty 0.0.18**.
The optional `agentic-evaluation` extra retains **Ragas 0.2.6**, the compatible
**LangChain community 0.3** adapter and **datasets >=5.0.1**. Ragas 0.4 conflicts with the
CLI Rich 15 baseline; datasets 5.1 conflicts with the Hugging Face integration's
`hf-xet<1.5` constraint. Preserve the complete graph when locking an application;
see the [hotfix dependency notes](../../dependency-updates.md). A future major upgrade needs separate adapter qualification.
