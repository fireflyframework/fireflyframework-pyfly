# Examples

Copyright 2026 Firefly Software Foundation. Licensed under the Apache License 2.0.

These migrated scripts use PyFly Agentic's public application APIs. Start with [model_agnostic_agent.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/model_agnostic_agent.py): it combines `Agent`, typed `ModelOptions`, a `@agent_tool`, conversation memory, Pydantic structured output, and streaming without importing a provider SDK.

## Setup and model selection

From the repository root, with Python 3.12+ and [uv](https://docs.astral.sh/uv/):

```bash
uv sync --extra agentic --extra agentic-dev
export PYFLY_AGENTIC_DEFAULT_MODEL="openai-responses:gpt-6-luna"
# Set OPENAI_API_KEY in your shell; never put credentials in source.
uv run python examples/agentic/model_agnostic_agent.py
```

For another provider, set its model identifier and credentials. Most model examples use `PYFLY_AGENTIC_DEFAULT_MODEL`, then the legacy `MODEL` variable, then `openai-responses:gpt-6-luna`. Exceptions are listed below. Scripts do not prompt for keys. Some scripts also load `.env` explicitly; shell variables work for all model examples. Provider authentication is checked when an agent makes a request.

`openai:` and `azure:` retain Chat Completions behavior in PyFly Agentic. Use `openai-chat:` / `azure-chat:` to select it explicitly, or `openai-responses:` / `azure-responses:` for Responses. Endpoint selection stays explicit. GPT-6 Astra tool calls require Responses; GPT-6 Sol/Luna Chat tool calls require reasoning to be disabled. Choose an endpoint and model that support the example's tools, structured output, and reasoning controls.

Use `ModelOptions` for application controls such as `max_tokens`, `reasoning`, and `store_responses`. Unsupported combinations raise `ModelOptionsError`; portability does not mean every model supports every setting. Provider-specific `model_settings` remain an advanced escape hatch. See the [model guide](models.md) and [migration guide](migration.md).

## Agent, memory, and evaluation examples

**Model** means configured provider credentials and billable requests are required. Model outputs and reported usage vary; examples do not guarantee a particular answer or cost.

| Script | Demonstration | Requirements and notes |
| --- | --- | --- |
| [model_agnostic_agent.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/model_agnostic_agent.py) | Framework tools, memory, structured output, streaming, per-run `ModelOptions` | Model; `PYFLY_AGENTIC_DEFAULT_MODEL` or explicit Responses fallback, without the legacy `MODEL` fallback |
| [basic_agent.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/basic_agent.py) | Create and run an agent; inspect metadata | Model |
| [conversational_memory.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/conversational_memory.py) | Multi-turn conversational agent with `MemoryManager` | Model; local memory |
| [classifier.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/classifier.py) | Categories and typed `ClassificationResult` | Model with structured output |
| [extractor.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/extractor.py) | Extract a Pydantic contact schema | Model with tools and structured output |
| [summarizer.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/summarizer.py) | Configure summary length, style, and format | Model with tools |
| [router.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/router.py) | Produce structured routing decisions | Model with structured output |
| [delegation_strategies.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/delegation_strategies.py) | Round robin, capability, cost-aware, content-based, and chained selection | Model; `MODEL_CHEAP`, `MODEL_MED`, `MODEL_EXPENSIVE` default to Responses GPT-6 Luna/Sol/Astra; routing itself may make requests; unknown prices are reported |
| [openai_responses.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/openai_responses.py) | Explicit Responses endpoint, tools, memory, structured output, streaming, storage disabled | `OPENAI_API_KEY`; `OPENAI_MODEL` is an unprefixed ID, default `gpt-6-luna` |
| [conversation_export_import.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/conversation_export_import.py) | Export/import conversation records, metadata, and summaries | Offline; constructs but does not invoke an LLM summarizer |
| [llm_eval_example.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/llm_eval_example.py) | Judge answer correctness and relevance | Model; `--model` overrides environment selection; optional `--items-file` JSONL; these metrics do not require the Ragas evaluation extra |
| [rubric_reviewer.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/rubric_reviewer.py) | Separate generator/grader and bounded revision loop | Model; optional `--question`; unsatisfied reviews raise after the retry limit |

## Reasoning examples

All reasoning scripts require a model that supports structured output. They make multiple calls, and may stop with a step-limit error when the model does not reach the required completion condition. Their traces contain application-generated intermediate steps, not access to hidden provider reasoning.

| Script | Demonstration |
| --- | --- |
| [reasoning_cot.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/reasoning_cot.py) | Structured thoughts and explicit completion |
| [reasoning_react.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/reasoning_react.py) | Reason/act/observe through `run_with_reasoning()` |
| [reasoning_reflexion.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/reasoning_reflexion.py) | Generate, critique, and revise |
| [reasoning_plan.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/reasoning_plan.py) | Create and execute a structured plan |
| [reasoning_tot.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/reasoning_tot.py) | Generate and score alternative approaches |
| [reasoning_goal.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/reasoning_goal.py) | Decompose a goal into phases and tasks |
| [reasoning_pipeline.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/reasoning_pipeline.py) | Compose chain-of-thought and reflexion patterns |
| [reasoning_memory.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/reasoning_memory.py) | Enrich reasoning with working memory |

## Local tools, workflows, and controls

These run without provider credentials. Simulated failures, latency, and usage records are explicitly identified fixtures used to exercise real framework controls.

| Script | Demonstration | Runtime notes |
| --- | --- | --- |
| [cached_tool.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/cached_tool.py) | Cache hits, expiry, invalidation, and eviction | Local lookup with simulated latency |
| [tool_timeout.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/tool_timeout.py) | Tool deadlines and timeout handling | Local functions with real short sleeps |
| [security_guards.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/security_guards.py) | Prompt/output scans, redaction, deny patterns, length limits | Scanners run locally; no model invocation |
| [observability_usage.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/observability_usage.py) | Usage aggregation, bounded records, cumulative cost | Illustrative records and prices, not provider invoices |
| [quota_management.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/quota_management.py) | `BudgetGate`, rate limiting, exponential retry delays | Known-cost records and local word-count operations; no provider spend |
| [circuit_breaker.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/circuit_breaker.py) | Closed/open/half-open transitions, recovery, cached fallback | Deterministic failure fixtures and real recovery waits |
| [prompt_caching.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/prompt_caching.py) | Cache statistics and hypothetical tariff arithmetic | Offline by default; `--live` makes six requests, using configured model or `anthropic:claude-sonnet-5`; cache hits are not guaranteed |
| [pipeline_branching.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/pipeline_branching.py) | Conditional DAG routing, progress events, retries | Rule-based local steps |
| [pipeline_state.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/pipeline_state.py) | State branching, document-statistics fan-out, checkpoint/pause/resume | Creates a real tar archive, verifies its digest, and copies it to a local release directory; approval is simulated; all artifacts are scoped to a temporary directory |

## Integration and service examples

| Script | Demonstration | Requirements and effects |
| --- | --- | --- |
| [batch_processing.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/batch_processing.py) | Bounded concurrency, callbacks, classification DAG, usage comparison | Model; approximately 43 requests across all demos; does not submit native provider batch jobs or obtain batch discounts |
| [incremental_streaming.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/incremental_streaming.py) | Buffered/incremental text fragments, debouncing, measured timing | Model with streaming; fragments are not billing tokens; timing is measured rather than guaranteed |
| [full_integration.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/full_integration.py) | Agent middleware, guards, local memory, streaming, batch DAG, usage | Model; eight requests; telemetry export needs host OTel setup; database storage and encryption are not configured here |
| [cost_tracking.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/cost_tracking.py) | Isolated usage ledger, budget rules, JSONL/OTel sinks | Model; new `firefly-cost-*` temporary directory retained for inspection; `--inflated-prices` changes local accounting only; OTel export needs host setup |
| [http_connection_pooling.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/http_connection_pooling.py) | Sequential/concurrent HTTP, POST/header echo, pooled/urllib timing | HTTP network; default `https://httpbin.org`; `HTTP_EXAMPLE_BASE_URL` may point to a compatible local echo service; no model |
| [database_persistence.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/database_persistence.py) | Native Data scoped working-fact cold application restart | `[agentic-relational]`; default temporary SQLite, TestModel only and no hosted calls. Optional native PostgreSQL URL uses validated existing schema unless creation is explicitly selected. |
| [mongodb_persistence.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/mongodb_persistence.py) | Working-fact cold restart using a borrowed native async Mongo client | `[agentic-mongodb]`, disposable MongoDB replica set; TestModel only and no hosted calls; explicit index provisioning and preserved database location. |
| [idp_pipeline.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/idp_pipeline.py) | PDF ingestion → splitting → classification → extraction → validation → assembly → explanation | Model with tools/structured output, network access to the sample PDF, `pdfplumber` from dev dependencies; multiple calls/retries; document counts depend on input and model |
| [idp_tools.py](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/idp_tools.py) | IDP schemas, PDF/section/date tools, prompt templates, validators | Support module for `idp_pipeline.py`; importing it makes no requests |
| [software_factory/](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/software_factory/) | Executable state-based software delivery workflow | See its [README](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/software_factory/README.md) for local execution and optional database adapters |

The relational example runs offline by default with `agentic-relational`. For
PostgreSQL add `agentic-postgres`, set `PYFLY_DATA_RELATIONAL_URL` and review
`PYFLY_DATA_RELATIONAL_DDL_AUTO` (external URL defaults to `validate`). Mongo uses
`PYFLY_DATA_DOCUMENT_URI` and `PYFLY_DATA_DOCUMENT_DATABASE`; a new collection can
explicitly enable `PYFLY_AGENTIC_MEMORY_CREATE_INDEXES=true`. Preserve existing
schema/database locations and complete [identity migration](migration.md#native-data-and-runtime-cutover)
first. Both examples use the native application lifetime and async memory APIs;
no Agentic-specific URL/pool variable or hosted-model credential is needed.
They restore typed working facts after restarting the ApplicationContext and
explicitly demonstrate that conversation history does **not** survive that
restart. Both clear only their generated demo working scope.

```bash
uv sync --extra agentic-relational
uv run python -m examples.agentic.database_persistence
```


## Offline verification

```bash
uv run pytest tests/agentic/integration/test_examples_offline.py
```

These checks verify import safety without credentials or network access, document and artifact processing, quota boundaries, cache arithmetic, actual HTTP requests against a local echo server, and agent/reasoning flows with explicit test-provider responses. They do not establish live provider quality, availability, or billing accuracy. Provider HTTP compatibility and credential-gated live tests are described in the [test guide](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/tests/agentic/catalogue.md).
