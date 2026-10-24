# Native Agentic tests

Run these commands from the PyFly repository root. The runtime lives in
`pyfly.agentic`; the standalone package and its workflows are historical. See the
[native migration guide](../../docs/modules/agentic/migration.md) for renamed APIs,
configuration, extras and application-owned persistence.

## Environment and default suite

Install the same optional dependencies and development tools used by CI:

```bash
uv sync --locked --all-extras --group dev --group agentic-dev
uv run --no-sync pytest tests/agentic -q
```

The default pytest selection excludes `integration`, `browser`, `nightly` and
`ollama`. It runs local fixtures, temporary SQLite databases, TestModel agents
and controlled HTTP transports without hosted provider credentials. `--no-sync`
keeps the explicitly provisioned environment for the commands below.

A test directory describes its purpose; its markers determine selection. Many
`tests/agentic/integration/` cases exercise internal wiring offline and therefore
run in the default suite. Real PostgreSQL cases carry `integration` and need Docker.

## Native Data and real PostgreSQL

`tests/agentic/integration/test_pgvector_store.py` and
`test_pgvector_native_data.py` use a disposable `pgvector/pgvector:pg16` container
and real drivers. They cover vector search, native memory, shared transactions,
rollback, schema policy and application ownership. The required native extra is
`agentic-vectorstores-pgvector`, included by the environment command above.

```bash
PYFLY_INTEGRATION_REQUIRE_DOCKER=1 uv run --no-sync pytest tests/agentic/integration/test_pgvector_store.py tests/agentic/integration/test_pgvector_native_data.py -m integration -q
uv run --no-sync pytest tests/integration/test_agentic_native_memory.py tests/integration/test_agentic_native_mongo_memory.py -m integration -q
```

Docker must be running; images may be downloaded. Fixtures create isolated state
and close their resources. Native SQLite checks are also in the default suite.
Mocked driver tests do not prove behavior against a deployed database or broker.

## Offline provider and documentation contracts

These use the actual Pydantic AI/provider SDK serialization path with controlled
HTTP responses or a local HTTP server. They check API selection, tools, structured
output, streaming, usage, memory replay, model options and connection reuse:

```bash
uv run --no-sync pytest tests/agentic/integration/test_openai_api_compatibility.py tests/agentic/integration/test_model_abstraction.py tests/agentic/integration/test_sync_http_connections.py -q
uv run --no-sync pytest tests/agentic/integration/test_documentation_contract.py tests/agentic/integration/test_examples_offline.py tests/agentic/examples -q
```

Documentation contracts check snippet syntax and native imports; executable
examples separately verify behavior. The software-factory tests use temporary
files to check generated code, build/QA results, release artifacts and recovery.
Controlled responses do not establish hosted model or account compatibility.

The optional `agentic-evaluation` extra (also included by `--all-extras`) enables
the Ragas adapters with deterministic model and embedding responses:

```bash
uv run --no-sync pytest tests/agentic/unit/evaluation -q
```

Ragas-specific checks skip without the extra. The compatible Ragas 0.2.6 and
LangChain Community 0.3.x constraints are explained in the
[native migration guide](../../docs/modules/agentic/migration.md).

For the documentation build and local link check:

```bash
uv run --no-project --with-requirements requirements-docs.txt python -m mkdocs build --strict
uv run --no-sync python scripts/check_agentic_docs.py --site-dir site
```

## Offline benchmarks

The 14 `performance/` cases measure mocked HTTP-tool overhead and local TestModel
streaming. They carry `nightly` to keep measurement work out of the PR suite:

```bash
uv run --no-sync pytest tests/agentic/performance -m nightly --benchmark-only -q
```

Measurements include framework overhead and teardown. They do not measure hosted
provider latency, prove network-pool reuse or enforce a cross-run timing threshold.
Use `--benchmark-disable` instead of `--benchmark-only` for a single functional
execution of each case without timing measurements.

## Explicit live model checks

The Anthropic and OpenAI suites make billable requests when valid credentials are
configured. They carry `nightly` and skip without a usable provider key. They are
not selected by the scheduled offline benchmark job.

```bash
uv run --no-sync pytest tests/agentic/integration/test_real_anthropic_e2e.py -m nightly -q
uv run --no-sync pytest tests/agentic/integration/test_real_openai_e2e.py -m nightly -q
```

Set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` before running the corresponding suite.
The fixture's dummy Anthropic key does not enable a live test. OpenAI model
selectors remain `FIREFLY_OPENAI_CHAT_MODEL` and `FIREFLY_OPENAI_RESPONSES_MODEL`;
both default to unprefixed `gpt-6-luna`. The Chat model must support function
calling with reasoning effort `none`. Tests cover tool execution, structured
output, streaming follow-up turns, serialized memory and usage/cost accounting.
A skipped suite does not establish provider compatibility or account access.

Local Ollama acceptance uses separately marked tests and installed local models:

```bash
uv run --no-sync pytest tests/agentic/test_ollama_local.py tests/agentic/test_native_runtime_restart.py -m ollama -q
```

See the [local Ollama tutorial](../../docs/modules/agentic/getting-started/ollama.md) for setup.

## Categories and CI

`unit/`, `integration/`, `functional/`, `security/`, `data_validation/`,
`responsible_ai/` and `examples/` cover component behavior, wiring, complete flows,
adversarial cases, schemas, transparency/privacy and runnable examples.
`performance/` contains the offline benchmarks described above.

[`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) runs lint, strict
mypy, the default suite on Python 3.12/3.13, documentation/book-source checks,
book companions and isolated wheel/sdist installations. Browser checks have their
own job and marker. Real database integration jobs run on daily 03:00 UTC schedules
and manual dispatch; the PR default suite excludes them. The scheduled/manual
`agentic-benchmarks` job measures only `tests/agentic/performance` on both supported
Python versions and uploads benchmark JSON. Hosted-provider and Ollama suites
require explicit runs in a prepared environment.

Do not use `-m 'not nightly'` as an offline shortcut: it overrides the default
marker expression and can include real integration, browser and Ollama cases.
