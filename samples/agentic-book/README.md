# Lumen PyFly Agentic book companion

Part VI of **PyFly by Example** (Chapters 19–21, English and Spanish) uses fictional
Lumen review guidance. No wallet funds or external accounts are changed. The real
`pyfly.agentic` runtime calls Pydantic AI's deterministic `TestModel`; no API key,
network model, external database, exporter, or embedding service is needed.
The native Data examples create temporary local SQLite files.

From this directory:

```bash
uv sync --extra dev --python 3.12
uv run pytest -q
```

Python 3.12 and 3.13 are supported targets; repeat with a separate environment for
each interpreter when qualifying a release. `pyfly` resolves to this checkout,
not a published release. Install `pyfly[agentic,agentic-relational]` for a packaged application with this SQLite journey.
`TestModel` is test infrastructure and is deliberately not a production model.

- `assistance.py`: typed output, read tool, native factory/bean/service, deferred approval.
- `knowledge.py`: explicit tenant/workspace vector scope, fixed fixture vectors.
- `native_memory.py`: application-owned SQLite datasource, native unit of work, async facts.
- `tests/test_native_memory.py`: real commit/rollback of business rows and working facts.
- `operations.py`: two native workflow model calls with phases and bounded budgets.
- `tests/test_journey.py`: registration identity, tool execution, stream closure,
  deferred effects, cross-tenant negative case, journal replay and budget exhaustion.

The two-dimensional vectors only make retrieval deterministic. They establish
scope behavior, not semantic relevance or provider quality. The approval test
uses an in-memory effect list: real commands require authenticated approval,
transactional authorization, idempotency and durable receipts. A workflow journal
replays model results; it does not make arbitrary Python side effects exactly once.

Books show current checkout APIs. Provider, persistent-store and production
acceptance must be qualified separately. A passing offline companion is not evidence
that every optional integration, real model or external consumer works.

For a live application, adapt `pyfly-live.yaml.example` through the configuration
profiles described in Chapter 3. Set `LUMEN_AGENT_MODEL` to the exact
provider/model identifier you are qualifying and supply that provider's credential
environment variables separately. In the production bean, omit the explicit
`TestModel` argument so the native factory uses the configured model. The checked-in
fixture bean deliberately keeps `TestModel`, so merely exporting credentials cannot
turn the offline tests into live calls. Validate live output/tool/approval behavior,
pricing, limits and persistence independently; the template has not been live-tested.


To invoke the explicitly selected local Ollama server at `127.0.0.1:11434`, run
`uv run python -m lumen_agentic.local_ollama`. It targets `qwen2.5:3b`; install that model on the local server before running it. The
`ModelSpec` fixes the `/v1` endpoint and the runtime closes its newly created HTTP
client. The synthetic local token prevents inheriting cloud credentials. This
command makes a real local request; the nine offline tests only build the local
adapter and continue to prohibit all real model requests. Live Ollama acceptance
is reported separately by the parent task's dedicated integration lane.

The native SQLite context uses `ddl-auto: create` only for its fictional local
schema. Production deployments use ordinary migrations with `none`/`validate`.
Await `MemoryManager.async_*` methods on the application loop; synchronous
database-backed facades cannot block that loop. Working facts persist;
conversation history remains process-local.

Run `uv run python -m lumen_agentic.native_memory` for the real SQLite demo.
It prints one committed business row, its dictionary fact, and a null rolled-back
fact, then shuts down the application and removes its temporary directory.
No model is constructed or called by this command.
