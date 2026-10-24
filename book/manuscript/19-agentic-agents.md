<span class="eyebrow">Chapter 19</span>

# Agents and Tools in Lumen {.chtitle}

Lumen's support team needs to explain wallet review policy. The assistant reads a fictional handbook, returns a typed answer and can request a human review. It cannot decide whether money moves. The wallet aggregate and command handlers from Chapters 6 and 7 remain responsible for that decision. This final part adds native `pyfly.agentic` capabilities to the same application structure you already know: configuration creates services, the container injects them, and the application owns their lifetime.

Our complete companion is `samples/agentic-book`. Its real Pydantic AI `TestModel` generates deterministic responses without credentials or provider requests. This makes the examples reproducible on Python 3.12 and 3.13. It does not establish the accuracy, cost or latency of a production model. The test data, policy IDs and effects are fictional.

## 19.1. Enable the module deliberately

Install the optional core with `pip install 'pyfly[agentic]'`. Base PyFly installations need no AI SDK. Enable managed integration in the effective configuration tree:

```yaml
pyfly:
  agentic:
    enabled: true
    observability-enabled: false
    instrumentation-include-content: false
```

Keep the module disabled in profiles that do not use it. Enabling it without the required optional dependencies produces a startup error with the installation remedy. Provider and infrastructure adapters use namespaced `agentic-*` extras; consult the native module's extras table before selecting one. Existing PyFly extras retain their meaning, so a web service can combine `web` with `agentic`.

The configuration prefix is `pyfly.agentic`; environment names begin with `PYFLY_AGENTIC_`, for example `PYFLY_AGENTIC_DEFAULT_MODEL`. Native binding follows Chapter 3's effective configuration, profiles and placeholders. The typed settings model is `AgenticConfig` from `pyfly.agentic.config`. Explicit agent model settings override application defaults. Leave provider credential names in their provider-defined environment variables; do not place secrets in manuscripts, sample YAML or prompts.

The companion passes `TestModel` explicitly. A live profile instead supplies a provider-supported model identifier and credentials, selected from the provider's current documentation. Treat that profile as a separate qualification lane. Swapping a model is an observable behavior change: recheck output schemas, tools, approval semantics, limits and failure handling.

::: figure art/figures/19-agentic-runtime.svg | Figure 19.1 — One application owns its assistant, registry and resource lifetime

## 19.2. Construct once, inject the service

`AgenticRuntime` owns configuration, registries and accounting for one application. The injectable `AgentFactory` creates agents attached to that runtime and registers them by default. Return a factory-created agent from a singleton `@bean`, or pass its application runtime explicitly; startup rejects unbound or foreign agents. The service and runtime registry then share the same object. Avoid retaining request-scoped agents in a long-lived registry.

::: listing samples/agentic-book/src/lumen_agentic/assistance.py | Listing 19.1 — A typed answer is a contract at the service boundary
class AssistanceReply(BaseModel):
    summary: str
    policy_id: str
    needs_human: bool
:::

The companion's `build_assistant(factory)` supplies that output type, a read-only policy tool and deterministic output arguments. `AssistanceConfiguration.lumen_assistant` returns it from a `@bean` method. `AssistanceService` receives a `Agent` in its constructor and exposes `explain(question) -> AssistanceReply`. Controllers need not know how a model is constructed. With several agent beans, use the qualifier conventions from Chapter 2 to remove ambiguity.

For a programmatic test or command-line task, construct the owner explicitly:

```python
from pyfly.agentic.config import AgenticConfig
from pyfly.agentic.runtime import AgenticRuntime

async def explain_offline():
    async with AgenticRuntime(
        config=AgenticConfig(observability_enabled=False)
    ) as runtime:
        assistant = build_assistant(runtime.agent_factory)
        result = await assistant.run("Explain wallet review policy")
        return result.output
```

`build_assistant` is imported from the companion in this sketch. An application-managed runtime uses PyFly startup and shutdown; do not create another runtime for every request. `runtime.activate()` selects an owner for nested operations when orchestrating manually. The agent itself carries its owner through a run. Chapter 21 returns to cancellation, cleanup and nested workflows.

## 19.3. Tools express capabilities

A tool description and typed parameters tell the model what it may request. They do not authorize access. The application must derive tenant and user identity from trusted authentication, validate arguments and enforce domain policy on every tool invocation. Keep reads and writes visibly separate.

::: listing samples/agentic-book/src/lumen_agentic/assistance.py | Listing 19.2 — A small read capability with a deterministic result
@agent_tool("lookup_policy", auto_register=False)
async def lookup_policy() -> str:
    """Read the fictional Lumen policy; never change wallet state."""
    return "lumen-review-v1: Review requires a human decision."
:::

Here `auto_register=False` prevents an import-time tool registration: the factory receives the tool explicitly. The agent itself remains registered in its application's registry. `ToolKit` groups tools; combinators can filter, prefix or wrap toolsets. `AgentTool`, `ToolBuilder`, guards, caching and sequential/fallback/conditional composition cover larger capabilities. A guard is a useful enforcement layer, but the underlying command still needs authorization and idempotency. Never retry an unknown write outcome blindly.

Structured output validates shape, not truth. Lumen must check that `policy_id` identifies an authorized retrieved revision and that `needs_human` agrees with its business rules. A fluent sentence with a valid schema can still be wrong. Chapter 20 connects the schema to retrieved evidence and output validation.

## 19.4. Conversations and bounded context

Attach a `MemoryManager` when an assistant needs history and pass a stable `conversation_id` to `run`. Choose IDs from trusted application context, scoped to the authenticated tenant and user. Avoid exposing another caller's message history through a guessed ID. Working memory, conversation history and persisted store state have different contracts: persisting working memory alone does not make all conversations durable.

Explicit `message_history` is useful for a caller-managed conversation. Do not combine it with automatic `conversation_id` injection. Summarization, compression and token limits reduce prompt growth; they also discard information. Preserve exact policy IDs, approval records and command receipts outside a model-generated summary. Test a fresh conversation and a continued one separately.

Model options may be supplied per agent and per invocation. Temperature, output-token limits and provider-specific reasoning controls vary by model. Explicit overrides take precedence over runtime defaults, and nullable options can intentionally clear a default. Avoid promising that a generic option works identically across providers. Use the native model-options reference for the supported fields and provider mappings.

## 19.5. Streaming has a lifetime

Streaming is an owned operation, including iteration and early termination. Await `run_stream` to obtain its asynchronous context manager, then consume it inside `async with`. The companion asserts that all text fragments reconstruct the deterministic answer:

```python
async with await assistant.run_stream("Explain the status") as stream:
    fragments = [part async for part in stream.stream_text(delta=True)]
text = "".join(fragments)
```

Use a text-output agent for this example; the companion's typed assistant returns `AssistanceReply` instead. A browser disconnect must leave the context and propagate cancellation so resources and capacity are released. Incremental display is provisional: do not treat a partial streamed answer as an approved command. Commit business effects through the existing domain service after validation. Chapter 15's correlation identifiers connect the request, tool calls and completion telemetry.

## 19.6. Pause for a human decision

The companion's second agent wraps `request_review` with `requires_approval=True`. Its fictional side effect appends to a list. The initial run returns deferred tool requests; the list remains empty. An authorized operator then approves the specific tool-call ID and the application resumes with `DeferredToolResults` and the paused result's `all_messages()`.

Use `is_deferred(result)` before treating `result.output` as the final answer. Approval requests are control data. Persist their identifiers, messages, authenticated decision and expiry in an application-owned store. On resume, pass explicit message history and omit `conversation_id`; automatic memory injection and explicit history are mutually exclusive. A denial should be tested as carefully as approval. The companion proves the approved path performs its effect once in that run; it does not implement a durable approval inbox.

Approval answers who allowed a proposed tool call. It does not replace command authorization or guarantee exactly-once payment execution. Recheck permissions and current business state at execution, use an idempotency key, and record a durable receipt. Chapter 12's transactional workflows and Chapter 21's Agentic journals address different boundaries.

## 19.7. Run the companion and inspect the boundary

From `samples/agentic-book`, run `uv sync --extra dev --python 3.12` and `uv run pytest -q`. Repeat with an isolated Python 3.13 environment when qualifying both interpreters. Tests exercise the real native runtime, registry identity, tool result in message history, typed output, stream completion and deferred effect. They make no live-provider calls.

An exercise: add an invalid policy ID to the deterministic output and reject it in the application service. Keep the fixture model deterministic, then add separate live-model evaluation cases. Before advancing, you should be able to explain which object owns the agent, which tool can cause an effect, and why a valid output model alone cannot authorize that effect. Chapter 20 supplies the evidence that makes the assistant useful.

## 19.8. Try a local Ollama model

PyFly Agentic also supports a local model through its typed model factory. The
following explicit opt-in targets an Ollama server at `127.0.0.1:11434` using
its OpenAI-compatible `/v1` endpoint. The example selects `qwen2.5:3b`,
which must be installed on that server. Availability,
structured output and tool behavior must be checked on the actual model.

::: listing samples/agentic-book/src/lumen_agentic/local_ollama.py | Listing 19.3 — Build a local model and attach it to the native runtime
async def build_local_assistant(factory: AgentFactory, model_name: str = "qwen2.5:3b") -> Agent:
    spec = ModelSpec(
        provider="ollama",
        model=model_name,
        base_url="http://127.0.0.1:11434/v1",
    )
    # A synthetic local token prevents inheriting a cloud credential from the environment.
    model = ModelFactory().build_with_secret(spec, "local-no-auth")
    factory.runtime.own(model.client)
    return factory.create(
        "lumen-local-assistant",
        model=model,
        instructions="Explain fictional Lumen policy. Never authorize a payment.",
        model_settings={"max_tokens": 128},
    )

:::

Run `uv run python -m lumen_agentic.local_ollama` from the companion directory
only when you intend to invoke the local server. This command makes a real local
model request; it is separate from `pytest`, whose fixture forbids real model
requests. The seventh companion test constructs the local adapter without
invoking it. The synthetic token carries no cloud credential, and the runtime
owns and closes the newly created HTTP client. Do not transfer ownership of a
shared external client without an explicit lifecycle decision.

`ModelSpec` binds provider, tagged model name and endpoint before `AgentFactory`
creates the agent. Explicit model objects override configured defaults. A local
response proves that request path; evaluate accuracy, tools and business
authorization separately.

## 19.9. Await memory on the application's event loop

A started application's database resources belong to its event loop. Use
`await memory.async_set_fact(key, value)`, `await memory.async_get_fact(key)` and
`await memory.async_get_working_context()` in asynchronous services. The underlying
`WorkingMemory` offers `async_set`, `async_get`, `async_items` and the corresponding
async operations. These methods also work with the in-memory fixture, allowing a
service to retain its interface when persistence changes.

Sync compatibility methods may bridge from another thread to the running owner
loop; calling a database-backed sync method on that same loop is rejected. Likewise,
use `await assistant.run(...)` on a started application's owner loop. `run_sync`
cannot block that loop while waiting for its own work. Chapter 21's stream context
must also be consumed on its owner loop.

The store persists working facts. Conversation history remains process-local,
including when the working store is relational or MongoDB. Restarting a worker
therefore does not restore its conversations. Keep durable approval records and
business receipts in the application's Data model. Chapter 20 demonstrates an
actual SQLite transaction containing both a business row and an async fact.
