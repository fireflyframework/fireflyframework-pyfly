# Local Ollama

Run PyFly Agentic against a local Ollama model with the same runtime, factory,
tools, conversations and orchestration APIs used by a hosted model. These are
real inference requests to a running local service. Deterministic `TestModel`
examples remain useful for offline contracts; they do not qualify a model server.

## Prepare the local environment

From the reviewed PyFly source checkout:

```bash
python -m pip install -e ".[agentic,agentic-ollama-embeddings]"
```

Install/start Ollama using its [official setup instructions](https://ollama.com/download).
Use the existing local server if one is already listening. Otherwise run
`ollama serve` in another terminal. Prepare the chat and embedding models:

```bash
ollama pull qwen2.5:3b
ollama pull nomic-embed-text
ollama list
curl -fsS http://127.0.0.1:11434/api/version
curl -fsS http://127.0.0.1:11434/api/tags
```

The version endpoint identifies the running server; `api/tags` lists installed
models. Exact model tags, quantization and hardware affect behavior and timing.
See Ollama's [model inventory API](https://docs.ollama.com/api/tags).

## Select the correct endpoint

| Use | Client base URL |
| --- | --- |
| Chat through `ModelSpec(provider="ollama", ...)` | `http://127.0.0.1:11434/v1` |
| Native Ollama embedding integration | `http://127.0.0.1:11434` |
| Acceptance-test server | `http://127.0.0.1:11434` |

Ollama exposes an [OpenAI-compatible chat endpoint](https://docs.ollama.com/api/openai-compatibility)
and a distinct [native embedding endpoint](https://docs.ollama.com/api/embed).
PyFly's model factory selects Chat Completions for provider `ollama` and supplies
the SDK's required placeholder credential for a keyless local endpoint.
Compatibility at the HTTP boundary does not establish every model's tool or
structured-output capabilities.

## A complete local agent

Save this script as `local_agent.py`. The script owns its newly constructed model
client and closes it after the runtime finishes; the runtime owns application
registries and execution state.

```python
import asyncio
import os

from pyfly.agentic.config import AgenticConfig
from pyfly.agentic.models import Credential, ModelFactory, ModelOptions, ModelSpec
from pyfly.agentic.runtime import AgenticRuntime


async def main() -> None:
    server = os.environ.get("PYFLY_OLLAMA_TEST_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    model_name = os.environ.get("PYFLY_OLLAMA_TEST_MODEL", "qwen2.5:3b")
    model = await ModelFactory().build(
        ModelSpec(
            provider="ollama",
            model=model_name,
            base_url=f"{server}/v1",
            credential=Credential.none(),
        )
    )
    config = AgenticConfig(
        observability_enabled=False,
        cost_tracking_enabled=False,
        plugin_auto_discover=False,
    )
    try:
        async with AgenticRuntime(config=config) as runtime:
            assistant = runtime.agent_factory.create(
                "local-lumen",
                model=model,
                model_options=ModelOptions(max_tokens=256, request_timeout=120.0),
            )
            result = await assistant.run("Explain one benefit of a wallet ledger in one sentence.")
            print(result.output)
    finally:
        await model.client.close()


if __name__ == "__main__":
    asyncio.run(main())
```

```bash
PYFLY_OLLAMA_TEST_MODEL=qwen2.5:3b python local_agent.py
```

Responses vary. Token limits and request timeouts bound different parts of the
request. Hosted-price accounting is disabled in this small example; that does not
measure or cap local compute cost. Keep shared model clients under their owner's
lifetime policy. See [native DI and lifecycle](../native-runtime.md).

## Native embeddings

`OllamaEmbedder` uses the native API at the server root. Its public asynchronous
context manager closes its owned HTTP client:

```python
import asyncio
import os

from pyfly.agentic.embeddings.providers.ollama import OllamaEmbedder


async def main() -> None:
    server = os.environ.get("PYFLY_OLLAMA_TEST_BASE_URL", "http://127.0.0.1:11434")
    model = os.environ.get("PYFLY_OLLAMA_TEST_EMBEDDING_MODEL", "nomic-embed-text")
    async with OllamaEmbedder(model=model, base_url=server) as embedder:
        vector = await embedder.embed_one("Lumen wallet ledger")
        assert vector
        print(f"Embedding dimensions: {len(vector)}")


if __name__ == "__main__":
    asyncio.run(main())
```

For application-owned lifetimes, `runtime.own(embedder)` transfers cleanup to the
runtime. An explicit `await embedder.aclose()` is also available and idempotent.
Ingestion/query vectors must use the same model and embedding space. Changing
models can require re-embedding existing documents. See [embeddings](../embeddings.md)
and [vector stores](../vectorstores.md) for retrieval and persistence scope.

## Run the native local acceptance lane

With development/test dependencies installed, select the actual model and server:

```bash
export PYFLY_OLLAMA_TEST_MODEL=qwen2.5:3b
export PYFLY_OLLAMA_TEST_BASE_URL=http://127.0.0.1:11434
export PYFLY_OLLAMA_TEST_EMBEDDING_MODEL=nomic-embed-text
python -m pytest tests/agentic/test_ollama_local.py -m ollama -v
```

These variables select the test environment; they are not `AgenticConfig` fields.
The base URL is the server root, without `/v1`. Record the server version, exact
model tags, test counts, failures and skips with the result. A passing lane qualifies
the exercised requests against that local installation. It does not qualify every
Ollama model, hosted provider, vector-store backend or deployment platform.

## Diagnose a failed local run

| Symptom | Check |
| --- | --- |
| Connection refused | Confirm server readiness and the base URL/port. |
| Model not found | Compare the selected tag with `ollama list` / `/api/tags`; pull the intended model if needed. |
| Chat 404 | Use the `/v1` compatibility base URL for `ModelSpec`; keep the test URL at the server root. |
| Embedding 404 | Use the server root; the native embedder calls `/api/embed`. |
| Empty/truncated answer or timeout | Check readiness, memory, inference time and token budget; an initial request may load the model. |
| Tool/schema validation error | Inspect the model's capabilities and actual response; successful plain chat does not establish tool or structured-output behavior. |
