<span class="eyebrow">Chapter 21</span>

# Agentic Workflows and Operations {.chtitle}

Lumen's assistance journey has two model stages: find relevant policy and draft guidance. Later it may add parallel checks, a validation loop and a human decision. `pyfly.agentic` provides both pipelines and a Python workflow DSL for those arrangements. They sit inside the application's operational boundary: configuration, budgets, quotas, resources and telemetry belong to its runtime.

These orchestration APIs differ from Chapter 12's transactional workflows and sagas. An Agentic journal can reuse model-call outputs. It does not make a wallet transfer durable or exactly once. Keep payment commands behind the existing transactional service and use its authorization, idempotency and receipt contracts.

## 21.1. Choose an orchestration shape

A pipeline makes stages, dependencies and state transitions explicit. The port-based DAG connects node outputs and inputs; state-based pipelines use a typed state model and return updates. `PipelineBuilder` constructs either mode. Agent, reasoning, embedding, retrieval and callable steps can be combined with branching, fan-out, fan-in and batch stages. Reducers determine how concurrent updates combine; choose them deliberately when several nodes write the same field.

State-based execution supports cycles with a recursion limit, routing and dynamic sends. `Pause` provides a control boundary, while checkpointers and audit logs expose restart and operator evidence. Define what state is persisted, how its schema evolves and which inputs identify a run before relying on resume. A checkpoint contains orchestration state; an external API call may have completed before the checkpoint was saved.

Choose the Python DSL when ordinary control flow expresses the journey more clearly. `workflow`, `Workflow`, `agent`, `parallel`, `pipeline`, `phase`, `human`, `stream` and `subworkflow` provide the orchestration primitives. Keep workflow definitions deterministic with respect to journaled call order. Renumbering or conditionally skipping calls can change what a sequence-keyed journal means.

::: figure art/figures/21-agentic-operations.svg | Figure 21.1 — Runtime ownership encloses bounded calls; durable business effects keep their own receipts

## 21.2. Run the smallest useful workflow

The companion constructs a `Workflow` directly so importing the module does not register anything in a process-global registry. A managed application can register its selected workflow during startup. Both stages execute real native agents using `TestModel`:

::: listing samples/agentic-book/src/lumen_agentic/operations.py | Listing 21.1 — Two named phases with deterministic offline model calls
def assistance_workflow() -> Workflow:
    async def explain(question: str) -> str:
        with phase("retrieve"):
            policy = await agent(
                f"Retrieve policy for {question}",
                model=TestModel(
                    custom_output_text="Review requires a human decision.",
                ),
            )
        with phase("draft"):
            return await agent(
                f"Draft guidance from: {policy}",
                model=TestModel(
                    custom_output_text="Read the policy; request human review.",
                ),
            )

    return Workflow("lumen-assistance", explain)
:::

This example teaches orchestration, not RAG quality: its first stage returns a fixture policy. In the full application, replace that stage with Chapter 20's authorized retrieval service and pass its evidence IDs into the typed drafting agent. Keep the same scope and validation contracts at the seam.

The default `ManagedAgentRunner` preserves the native agent stack. `DefaultAgentRunner` is the lighter direct Pydantic AI path; it has a different integration boundary. Runner protocols permit testing and custom integrations. Select a runner consciously rather than assuming every runner applies the same middleware or telemetry.

## 21.3. Budgets contain one run

A `WorkflowBudget` limits concurrent agents and total agent calls; optional ceilings cover tokens, USD cost and wall time. Lumen's two-stage fixture sets `max_concurrent_agents=1` and `max_agents_total=2`. Reducing total calls to one raises `WorkflowBudgetError` before the second stage starts. That assertion is an executable runaway-work backstop.

Token and cost counters are observed as calls finish, so a ceiling is not a provider-side guarantee that an in-flight call cannot exceed its estimate. Apply provider request limits and conservative headroom as well. Unknown pricing must follow an explicit policy; treating an unknown model as free can invalidate a spending guarantee. Record pricing assumptions when evaluating a new model.

Budgets, global cost guards and quotas solve related but different problems. A workflow budget contains one orchestration. A runtime ledger measures its application's usage. Quotas enforce the configured subject and time-window policy, potentially requiring a shared backend for multiple workers. An in-memory quota cannot establish a fleet-wide ceiling. Test rejection, concurrent requests, release after cancellation and reset behavior against the selected implementation.

## 21.4. Journals support replay, with limits

Pass a `Journal` to `workflow.run`. Reusing it returns completed model-call outputs instead of repeating those calls. The companion runs the same two-stage workflow twice and confirms that two entries produce the same final answer. Use a fresh journal for the budget-exhaustion test so cached outputs cannot hide the second call.

`FileJournalBackend` provides file-backed persistence keyed by a stable run key. The journal's persistence is best-effort: failures may be logged rather than propagated as a durable commit failure. Inspect and qualify that behavior before treating it as a compliance record. Use a storage design with explicit write guarantees for decisions that require durable evidence. Persist schema and workflow version alongside the run identity and reject incompatible resumes.

Resume re-executes ordinary Python around cached calls. Sending an email or changing a wallet balance between journaled calls can therefore repeat after a crash. Use an idempotent domain command with a durable key, an outbox or another explicit side-effect protocol. Parallel branches also need stable identities and deliberate failure policy. A replayed model result is useful evidence of a prior computation, not a transactional receipt.

The `human` primitive pauses by raising `WorkflowInterrupt`; supply the recorded decision and rerun with the same journal. This workflow-level pause differs from Chapter 19's deferred tool approval. In both cases the application must authenticate the operator, bind the decision to the exact request, enforce expiry and preserve the audit record.

## 21.5. Own lifecycle and cancellation

Create one `AgenticRuntime` per application. Native auto-configuration contributes the runtime and injectable services when enabled, respecting user-provided beans. Manual programs can use `async with AgenticRuntime(config=...)`; `runtime.activate()` selects that owner for nested pipeline/workflow calls. Selection must cover iteration and background work, not just startup.

At shutdown, stop accepting new work, drain or cancel owned operations, close streams, release capacity and close owned clients. Then stop owned background loops and dispose of references. External clients remain externally owned unless ownership was transferred explicitly. Shared store forks must not close the same client twice. Application restart and partial startup failure need their own tests.

Do not call blocking sync bridges from the event loop that owns the asynchronous operation. Consume streams on their owner event loop; starting a stream from a different loop is rejected. A non-streaming call from another thread can bridge to a running owner loop. Prefer async service methods in HTTP paths. When a request is cancelled, let cancellation propagate after cleanup; swallowing it can leave an apparently finished request consuming resources. Chapter 19's `async with` stream scope is part of that discipline.

## 21.6. Observe behavior without collecting everything

Use Chapter 15's application tracing and metrics providers. The native instrumentation scope is `pyfly.agentic`; do not install another global SDK or duplicate root logging handlers for each agent. Correlate request ID, application, workflow/run, agent, tool and policy revision. Avoid unbounded labels such as complete prompts or arbitrary user input.

`instrumentation_include_content` is opt-in. Token totals, durations and outcome classifications are often sufficient for operations; prompt/response capture needs a deliberate retention and access policy. Usage records, quota decisions, workflow events, pipeline audit logs and explainability reports answer different questions. Decide which record supports troubleshooting and which record is authoritative for business review.

Evaluation and experiments add structured cases, metrics, runners, comparison and storage. Optional evaluator adapters have their own dependency and provider constraints. Benchmarking in the lab can compare latency and cost, but compare representative inputs under the same configuration. Record dataset revision, model identifier, runtime settings, validator versions, sample size and failure distribution. Never present an offline deterministic result as a live-provider benchmark.

## 21.7. Qualify the service journey

Run the companion tests from Chapter 19. They prove native registration, typed output, read-tool invocation, streaming, approved deferral, vector scope isolation, journal replay and a total-call budget. The broader framework suite is responsible for multi-application isolation and lifecycle edge cases; production acceptance also needs the actual provider and persistence lanes.

Use a staged deployment profile with conservative budgets, content telemetry disabled, trusted identity propagation and an operator escalation path. Exercise missing credentials, unavailable vector storage, quota exhaustion, provider timeout, early stream disconnect and duplicate approval delivery. Confirm that failures preserve a useful user outcome without silently authorizing an action.

The complete API and adapter catalog is the native module reference in `docs/modules/agentic/`; this part supplies a practical journey through it. Lumen's domain model, commands, events, identity and deployment practices remain applicable. The assistant can explain evidence and coordinate bounded work while the existing business services retain authority over money and durable effects.

## 21.8. Reuse native HTTP, events and managed plugins

The built-in `HttpTool` receives the application's `HttpClientPort`. Configure
its transport under `pyfly.client`; the removed Agentic HTTP-pool fields are
rejected rather than creating an independent pool. The tool still needs explicit
host guards and allowlists. Provider SDK clients have their own model-specific
integration and ownership contract; a native tool transport does not reconfigure
every provider SDK.

Real agent runs, tools and reasoning publish awaited `AgentEvent` objects through
the owning application's `ApplicationEventPublisher`. Subscribers therefore run
with the native transaction context, including transaction-phase listeners.
Publication is best effort: listener or optional EDA-export failures are logged;
these operational events are not durable business receipts. EDA export requires
an explicit publisher and destination. Keep authoritative commands and outbox
records in the domain transaction.

Managed plugins use the application's `Container` for dependency resolution and
startup/shutdown lifecycle. Entry-point discovery is controlled by
`plugin-auto-discover`; the offline companion disables it for reproducibility.
Inject dependencies through the container instead of constructing a second HTTP
client, datasource or process-global runtime inside a plugin. During shutdown,
Agentic drains owned operations before PyFly Data disposes its borrowed resources.

## 21.9. Inspect the native Agentic admin view

With admin enabled, the **Agentic** sidebar and command-palette entry appear when
this application's container has an `AgenticRuntime`. The screen searches agents,
tools and registered workflow definitions; detail dialogs expose safe inventory
fields. **Refresh** fetches a new snapshot. It shows retained usage, requests,
tokens, estimated cost, latency and quota information. Registered workflows are
definitions, not execution history. A zero cost may mean no model price is known.

::: figure art/screenshots/21-agentic-admin.png | Figure 21.2 — Native admin snapshot from the offline TestModel fixture; labels and values are illustrative

The guarded GET endpoints are `/admin/api/agentic`,
`/admin/api/agentic/agents/{name}` and `/admin/api/agentic/tools/{name}`. Enable
`pyfly.admin.require_auth` and the permitted `allowed_roles` according to Chapter
15; an absent runtime or item returns 404. This view cannot run agents, mutate
registries or delete data. It omits prompts, instructions, messages, correlation
IDs, tool argument/default values, provider credentials/configuration and raw
quota-model keys. Treat the console's operational inventory as protected data.

The current dashboard uses the canonical Firefly brand kit's Python family
identity: locally served Manrope, charcoal, paper and amber. The screenshot records
the actual console, including its outlined pyfly endorsement. Both themes,
keyboard focus and mobile navigation are supported. The companion's nine offline
tests qualify its runtime and SQLite journey; admin browser acceptance and real
PostgreSQL/MongoDB/pgvector qualification remain separate framework test lanes.
