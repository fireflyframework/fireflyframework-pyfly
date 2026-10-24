# Agentic configuration reference

The native module binds **`pyfly.agentic`** to `AgenticConfig`. Activation
is separate: `pyfly.agentic.enabled` defaults to `false`; its environment switch is
`PYFLY_AGENTIC_ENABLED`. Installing dependencies does not enable it.

The table lists every field in the native typed model. Preserve underscores in
YAML and environment keys. `None` below is a Python default; use YAML `null` or
JSON `null` for a nullable override. Keep provider credentials in provider
environment variables rather than configuration examples.

```yaml
pyfly:
  agentic:
    enabled: true
    default_model: openai-responses:gpt-6-luna
    default_temperature: null
    quota_enabled: true
    quota_rate_limits:
      "openai-responses:gpt-6-luna": 30
```

The native binder handles field names and parses JSON values before typed validation:

```bash
export PYFLY_AGENTIC_ENABLED=true
export PYFLY_AGENTIC_DEFAULT_TEMPERATURE=null
export PYFLY_AGENTIC_QUOTA_RATE_LIMITS='{"openai-responses:gpt-6-luna": 30}'
```

Quota dictionary keys are model identifiers, and values are requests per minute.
See the supported controls in the [observability guide](observability.md).
These limits are local runtime controls, not provider account quotas.

| Field | Type | Default | Environment | Purpose |
| --- | --- | --- | --- | --- |
| `default_model` | `str` | `'openai:gpt-4o'` | `PYFLY_AGENTIC_DEFAULT_MODEL` | The default Pydantic AI model identifier used when no model is specified. |
| `default_temperature` | `float \| None` | `None` | `PYFLY_AGENTIC_DEFAULT_TEMPERATURE` | Default sampling temperature applied to every agent's model settings when the caller does not specify one |
| `max_retries` | `int` | `3` | `PYFLY_AGENTIC_MAX_RETRIES` | Default maximum number of retries for agent runs and tool calls. |
| `reasoning_output_mode` | `Literal['tool', 'native', 'prompted'] \| None` | `None` | `PYFLY_AGENTIC_REASONING_OUTPUT_MODE` | Structured-output strategy for reasoning patterns' internal LLM calls (plan / thought / reflection / evaluation / decomposition) |
| `observability_enabled` | `bool` | `True` | `PYFLY_AGENTIC_OBSERVABILITY_ENABLED` | Whether OpenTelemetry instrumentation is active. |
| `native_instrumentation_enabled` | `bool` | `True` | `PYFLY_AGENTIC_NATIVE_INSTRUMENTATION_ENABLED` | Whether to enable pydantic-ai's **native** OpenTelemetry instrumentation on every agent |
| `instrumentation_include_content` | `bool` | `False` | `PYFLY_AGENTIC_INSTRUMENTATION_INCLUDE_CONTENT` | Whether native instrumentation records prompt/response **content** (and tool args/results, binary content) as span attributes |
| `instrumentation_version` | `Literal[1, 2, 3, 4, 5, 6]` | `5` | `PYFLY_AGENTIC_INSTRUMENTATION_VERSION` | GenAI semantic-convention version for native instrumentation span names and attributes |
| `instrumentation_event_mode` | `Literal['attributes', 'logs']` | `'attributes'` | `PYFLY_AGENTIC_INSTRUMENTATION_EVENT_MODE` | Compatibility field; only attributes is accepted. logs fails validation. |
| `log_level` | `str` | `'INFO'` | `PYFLY_AGENTIC_LOG_LEVEL` | Default for explicit configure_logging(level=None); host logging stays host-owned. |
| `plugin_auto_discover` | `bool` | `True` | `PYFLY_AGENTIC_PLUGIN_AUTO_DISCOVER` | Discover and construct managed entry-point plugins at startup. |
| `artifact_cache_root` | `str` | `'~/.cache/pyfly.agentic/dbstore'` | `PYFLY_AGENTIC_ARTIFACT_CACHE_ROOT` | Local SQLite artifact working-copy root; explicit cache paths take precedence. |
| `default_chunk_size` | `int` | `4000` | `PYFLY_AGENTIC_DEFAULT_CHUNK_SIZE` | Default maximum number of estimated tokens per chunk. |
| `default_chunk_overlap` | `int` | `200` | `PYFLY_AGENTIC_DEFAULT_CHUNK_OVERLAP` | Default token overlap between consecutive chunks. |
| `max_context_tokens` | `int` | `128000` | `PYFLY_AGENTIC_MAX_CONTEXT_TOKENS` | Maximum context window size in tokens. |
| `qos_min_confidence` | `float` | `0.7` | `PYFLY_AGENTIC_QOS_MIN_CONFIDENCE` | Minimum confidence threshold for QoS guards. |
| `qos_consistency_runs` | `int` | `3` | `PYFLY_AGENTIC_QOS_CONSISTENCY_RUNS` | Number of runs for consistency checking. |
| `cost_tracking_enabled` | `bool` | `True` | `PYFLY_AGENTIC_COST_TRACKING_ENABLED` | Whether usage and cost tracking is active. |
| `budget_limit_usd` | `float \| None` | `None` | `PYFLY_AGENTIC_BUDGET_LIMIT_USD` | Hard budget limit in USD. When exceeded, a ``BudgetExceededError`` is raised (it is a hard gate, not a warning). The gate enforces only over models that can be **priced** |
| `cost_strict` | `bool` | `False` | `PYFLY_AGENTIC_COST_STRICT` | When true, `resolve_cost` raises `UnknownModelCostError` instead of returning ``None`` when no resolver can price the model. Equivalent env var: ``PYFLY_AGENTIC_COST_STRICT``. |
| `memory_backend` | `Literal['in_memory', 'file', 'relational', 'postgres', 'mongodb']` | `'in_memory'` | `PYFLY_AGENTIC_MEMORY_BACKEND` | Explicit working-fact backend; unknown selectors fail validation. |
| `memory_max_conversation_tokens` | `int` | `128000` | `PYFLY_AGENTIC_MEMORY_MAX_CONVERSATION_TOKENS` | Maximum tokens retained in conversation memory per conversation. |
| `memory_summarize_threshold` | `int` | `10` | `PYFLY_AGENTIC_MEMORY_SUMMARIZE_THRESHOLD` | Number of evicted turns before auto-summarization triggers. |
| `memory_file_dir` | `str` | `'.firefly_memory'` | `PYFLY_AGENTIC_MEMORY_FILE_DIR` | Directory for file-based memory persistence. |
| `memory_datasource` | `str \| None` | `None` | `PYFLY_AGENTIC_MEMORY_DATASOURCE` | Relational datasource name; null selects native module/primary datasource. Mongo uses document beans. |
| `memory_table` | `str \| None` | `None` | `PYFLY_AGENTIC_MEMORY_TABLE` | Relational table override; null uses pyfly_agentic_memory or memory_entries for postgres. |
| `memory_schema` | `str \| None` | `None` | `PYFLY_AGENTIC_MEMORY_SCHEMA` | Schema for relational backend; null uses datasource default. |
| `memory_create_table` | `bool \| None` | `None` | `PYFLY_AGENTIC_MEMORY_CREATE_TABLE` | null follows native ddl-auto; false requires migrations; true also requires create/create-drop. |
| `memory_create_indexes` | `bool` | `True` | `PYFLY_AGENTIC_MEMORY_CREATE_INDEXES` | Create Mongo indexes at startup; false verifies migration-provisioned indexes. |
| `memory_postgres_schema` | `str` | `'firefly_memory'` | `PYFLY_AGENTIC_MEMORY_POSTGRES_SCHEMA` | Preserved postgres memory schema; independent of memory_schema. |
| `memory_mongodb_database` | `str` | `'firefly_memory'` | `PYFLY_AGENTIC_MEMORY_MONGODB_DATABASE` | Preserved Mongo memory database on injected framework client. |
| `memory_mongodb_collection` | `str` | `'entries'` | `PYFLY_AGENTIC_MEMORY_MONGODB_COLLECTION` | Preserved Mongo memory collection. |
| `usage_tracker_max_records` | `int` | `10000` | `PYFLY_AGENTIC_USAGE_TRACKER_MAX_RECORDS` | Maximum number of usage records retained in memory. Oldest records are evicted when this limit is reached (FIFO). |
| `quota_enabled` | `bool` | `False` | `PYFLY_AGENTIC_QUOTA_ENABLED` | Whether API quota management and rate limiting is active. |
| `quota_budget_daily_usd` | `float \| None` | `None` | `PYFLY_AGENTIC_QUOTA_BUDGET_DAILY_USD` | Daily spending budget in USD. When exceeded, requests are rejected. |
| `quota_rate_limits` | `dict[str, int]` | `{}` | `PYFLY_AGENTIC_QUOTA_RATE_LIMITS` | Per-model rate limits as ``{model: requests_per_minute}``. Example: ``{"openai:gpt-4o": 60, "openai:gpt-4o-mini": 200}``. |
| `quota_adaptive_backoff` | `bool` | `True` | `PYFLY_AGENTIC_QUOTA_ADAPTIVE_BACKOFF` | Whether to use adaptive exponential backoff for 429 (rate limit) responses. |
| `rate_limit_max_retries` | `int` | `3` | `PYFLY_AGENTIC_RATE_LIMIT_MAX_RETRIES` | Max retry attempts for HTTP 429 rate limit errors. |
| `rate_limit_base_delay` | `float` | `1.0` | `PYFLY_AGENTIC_RATE_LIMIT_BASE_DELAY` | Base delay (seconds) for rate limit exponential backoff. |
| `rate_limit_max_delay` | `float` | `60.0` | `PYFLY_AGENTIC_RATE_LIMIT_MAX_DELAY` | Maximum delay (seconds) between rate limit retries. |
| `encryption_enabled` | `bool` | `False` | `PYFLY_AGENTIC_ENCRYPTION_ENABLED` | Wrap automatically composed storage with authenticated content encryption. |
| `encryption_key` | `str \| None` | `None` | `PYFLY_AGENTIC_ENCRYPTION_KEY` | AES-GCM key/password; an injected EncryptionProvider can supply encryption instead. |
| `embedding_batch_size` | `int` | `100` | `PYFLY_AGENTIC_EMBEDDING_BATCH_SIZE` | Maximum number of texts per embedding API call. |
| `embedding_max_retries` | `int` | `3` | `PYFLY_AGENTIC_EMBEDDING_MAX_RETRIES` | Maximum retries for embedding API calls. |

Settings are application-owned. `get_config()` resolves the active runtime;
`AgenticConfig(...)` supplies explicit typed settings for a script or user
bean. See [native DI/lifecycle](native-runtime.md), [PyFly configuration layering](../configuration.md),
and [migration](migration.md). Retained storage path/schema names are data locations,
not old Python import compatibility. Changing them requires an explicit data move.


## Removed controls and native replacements

The native binder rejects these former fields in configuration or
`PYFLY_AGENTIC_*` environment variables, and direct `AgenticConfig` construction
rejects them too. Remove each obsolete key and configure its replacement.

| Removed field(s) | Replacement |
| --- | --- |
| `memory_postgres_url`, `memory_postgres_pool_size`, `memory_postgres_pool_min_size` | Native `pyfly.data.relational` connection/pool settings plus `memory_datasource`. |
| `memory_mongodb_url`, `memory_mongodb_pool_size` | Native `pyfly.data.document` URI/client settings. |
| `http_pool_enabled`, `http_pool_size`, `http_pool_max_keepalive`, `http_pool_timeout` | Inject `HttpClientPort`; configure native `pyfly.client` settings, including timeout. |
| `tools_sandbox_enabled` | Explicit tool guards, host restrictions and allowlists. |
| `experiment_storage` | Explicit `ExperimentTracker`; SQL persistence needs an implemented native Data adapter. |
| `prompt_templates_dir` | Explicit `PromptLoader.from_directory(path)`. |
| `validation_enabled` | Explicit agent or pipeline output validators. |
| `default_embedding_model` | Explicit provider embedder constructor and `model=`. |
| `default_vector_store` | Explicit injected `VectorStoreProtocol` adapter. |
| `vector_store_namespace` | Explicit `namespace=` on vector operations or a scoped adapter. |

Earlier removed settings `cost_calculator`, `budget_alert_threshold_usd`,
`auth_api_keys`, `auth_bearer_tokens`, `cors_allowed_origins`, `otlp_endpoint`,
`rbac_enabled`, `rbac_jwt_secret` and `rbac_multi_tenant` are also rejected by
model validation. Use the cost resolver/explicit budget APIs, host HTTP/security
configuration and host observability configuration as appropriate.
`instrumentation_event_mode` remains a compatibility field with only
`attributes` accepted; `logs` fails validation. Legacy instrumentation versions
1–4 normalize to version 5 when instrumentation is constructed.

`FIREFLY_DBSTORE_CACHE_ROOT` is rejected by native binding. Move its exact path
to `pyfly.agentic.artifact_cache_root` / `PYFLY_AGENTIC_ARTIFACT_CACHE_ROOT`
to keep the existing artifact cache location. Existing files are not moved.

## Core configuration API migration

Use the framework's effective configuration tree and native binder:

```python
from pyfly.agentic.auto_configuration import bind_agentic_config
from pyfly.core.config import Config

framework = Config.from_file("pyfly.yaml")
agentic = bind_agentic_config(framework)
assert agentic.memory_backend in {"in_memory", "file", "relational", "postgres", "mongodb"}
```

`Config.from_file(...)` and `Config.from_sources(...)` load framework files and
profiles; `Config(data)` accepts an explicit tree. `Config()` alone loads no
application file. `Config.get(...)` reads a value, `Config.bind(...)` binds a typed
model, and `effective_dict()` / `effective_section(...)` expose effective values.
Use `bind_agentic_config(...)` for Agentic JSON/nullable environment parsing and
removed-key checks. `AgenticConfig(...)` takes explicit values and performs typed
validation; it has no Settings `.env`, `_env_file` or independent file loader.
`get_config()` selects the active runtime; configure that runtime or inject its
config instead of replacing another application's settings.
