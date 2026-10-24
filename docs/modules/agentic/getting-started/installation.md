---
title: Agentic installation
description: Install PyFly's opt-in agentic module and only the integrations your application uses.
---

# Agentic installation

PyFly Agentic lives in the **`pyfly` distribution**, under **`pyfly.agentic`**.
Use Python **3.12 or 3.13**. Provider SDKs, databases and binary converters have
additional platform requirements; a universal framework wheel does not qualify
those dependencies on every operating system.

## From a release

This guide uses **v26.10.11** (package version `26.10.11`). Verify the release's
assets before installing its wheel. PyFly is distributed through
[GitHub Releases](https://github.com/fireflyframework/fireflyframework-pyfly/releases),
not PyPI. Install the wheel and select the optional module explicitly:

```bash
python -m pip install "pyfly[agentic] @ https://github.com/fireflyframework/fireflyframework-pyfly/releases/download/v26.10.11/pyfly-26.10.11-py3-none-any.whl"
# In a uv-managed application:
uv add "pyfly[agentic] @ https://github.com/fireflyframework/fireflyframework-pyfly/releases/download/v26.10.11/pyfly-26.10.11-py3-none-any.whl"
```

Add namespaced extras such as `agentic-postgres` or `agentic-ollama-embeddings`
to the same bracketed selection when needed. Earlier PyFly releases do not include
this module. Preserve the selected release URL and your resolved dependency lock.

## From source

From the PyFly checkout containing `src/pyfly/agentic`:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[agentic]"
```

With uv, use `uv sync --extra agentic`. Add each required integration by its full
namespaced extra. For example:

```bash
python -m pip install -e ".[agentic,agentic-postgres,agentic-openai-embeddings]"
# Equivalent dependency selection with uv:
uv sync --extra agentic --extra agentic-postgres --extra agentic-openai-embeddings
```

These commands run from the framework source root, not a fresh application folder.
For a separate application, install that reviewed checkout as a local dependency
or build and install its wheel. Record its commit and lock the resolved graph.
The former standalone installer installs the former distribution; use PyFly's
[installer guidance](../../../installation.md) for the framework, or the explicit
source commands above for this opt-in module.

## Scaffold or update an application

With the PyFly CLI installed, select **agentic** in `pyfly new`'s interactive
feature picker, or create a minimal service directly:

```bash
pyfly new agent-service --archetype core --features agentic
```

The generated dependency selects `pyfly[agentic]` with the official release wheel
URL pinned to the installed CLI version. `pyfly.yaml` explicitly sets
`pyfly.agentic.enabled: true`. If the CLI came from an unreleased source checkout,
the matching release wheel must be published before resolving that dependency.
No model, credentials, database or external backend is selected for you.
Inject `AgentFactory` from
`pyfly.agentic.factory` into services; pass a model explicitly or configure
`pyfly.agentic.default_model` before running agents.

For an existing application, run `pyfly add agentic`, resolve dependencies, and
add the activation setting shown below. `pyfly features` shows the selected
extra; `pyfly remove agentic --yes` removes it. Existing direct references and
environment markers are preserved. Without an existing direct reference,
unversioned PyFly dependencies are automatically pinned to the CLI version's
official wheel. A non-direct version constraint is rejected before writing:
review that constraint and explicitly choose a compatible official wheel.
Hatchling projects gain the direct-reference metadata setting when absent; an
explicit policy disabling it requires updating that setting before retrying.
Disable Agentic before removing its dependencies. Add storage and
integration extras explicitly to the same PyFly dependency as needed.

## Optional extras

| Extra | Adds / use |
| --- | --- |
| `agentic` | Agents, tools, typed model configuration, reasoning, memory, orchestration, telemetry and Pydantic AI provider integrations |
| `agentic-relational` | Agentic plus native PyFly Data relational support, including SQLite |
| `agentic-postgres` | Agentic plus native Data relational support and PostgreSQL driver |
| `agentic-mongodb` | Agentic plus native Data document support using async PyMongo, without Motor |
| `agentic-security` | cryptography for at-rest encryption |
| `agentic-script-execution` | Monty 0.0.23 SDK and its runtime binary for local sandbox workers |
| `agentic-embeddings` | NumPy for in-memory vector math |
| `agentic-openai-embeddings` | OpenAI embedding client |
| `agentic-azure-embeddings` | Azure embedding client |
| `agentic-cohere-embeddings` | Cohere embedding client |
| `agentic-google-embeddings` | Google embedding client |
| `agentic-mistral-embeddings` | Mistral embedding client |
| `agentic-voyage-embeddings` | Voyage embedding client |
| `agentic-bedrock-embeddings` | AWS Bedrock embedding client |
| `agentic-ollama-embeddings` | Ollama HTTP embedding client |
| `agentic-vectorstores-chroma` | Chroma vector-store client |
| `agentic-vectorstores-sqlite-vec` | sqlite-vec local store |
| `agentic-vectorstores-pinecone` | Pinecone vector-store client |
| `agentic-vectorstores-qdrant` | Qdrant vector-store client |
| `agentic-vectorstores-pgvector` | Agentic plus native Data relational/PostgreSQL dependencies for borrowed pgvector sessions |
| `agentic-watch` | watchfiles source watching |
| `agentic-binary` | PDF, image, HEIF, archive and Outlook-message normalization |
| `agentic-reasoning-eval` | NumPy / pandas reasoning comparisons |
| `agentic-evaluation` | Ragas / LangChain evaluation adapters; see [constraints](../migration.md#optional-evaluation-dependencies) |
| `agentic-dev` | Migrated Agentic development tools |
| `agentic-all` | Runtime integrations; excludes reasoning-eval, evaluation and dev |

PyFly's existing extras such as `postgres`, `security`, `full`, and `dev` retain
their existing meanings. Use the `agentic-` prefix for Agentic integrations. Full
framework installation does not enable Agentic; installation and activation are
separate choices. Optional external tools such as LibreOffice and Gotenberg are
still required only by the binary converter that uses them.

## Activate and verify

For a PyFly application, enable the module explicitly:

```yaml
pyfly:
  agentic:
    enabled: true
    default_model: openai-responses:gpt-6-luna
```

Provider credentials stay in environment variables such as `OPENAI_API_KEY` or
`ANTHROPIC_API_KEY`. Test models need no credentials. The module is disabled by
default. Enabling it without the `agentic` dependencies produces a startup error
that identifies the missing installation.

```bash
python -c "from importlib.metadata import version; from pyfly.agentic.agents import Agent; print(version('pyfly'))"
```

## Uninstall

Remove the `pyfly` distribution from this environment with
`python -m pip uninstall pyfly`, or remove the local dependency from your application
manifest and resolve again. This removes the whole framework package; individual
extras are dependency selections, not separately uninstallable distributions.
Preserve application data and external resources according to your retention policy.

Next: [native DI and lifecycle](../native-runtime.md),
[quick start](quickstart.md), or [migration](../migration.md).

## Monty interpreter compatibility

The native `agentic-script-execution` extra pins **`pydantic-monty==0.0.23`**
and installs its `pydantic-monty-runtime` binary dependency. The adapter uses an
owned worker pool and a fresh session per run; it closes both after success,
failure or cancellation. Guest scripts keep direct calls to registered sync and
async tools without guest-side `await`. Binary discovery follows the SDK's
`MONTY_BIN`/installed-binary locator. A missing binary fails explicitly; no
CPython or remote-service fallback is enabled.

Monty 0.0.23 removed `max_allocations`. Remove any non-`None` value from
`ExecutionLimits`; PyFly raises `ValueError` before starting workers rather than
silently ignoring it. Use wall-clock, memory and recursion budgets. The
[execution guide](../execution.md) describes callback scheduling and lifecycle.

## Qualified dependency baseline

Native Agentic pins **Pydantic AI 2.59.0**, matching the source-qualified API and
provider contracts. `agentic-script-execution` pins **pydantic-monty 0.0.23**.
The optional `agentic-evaluation` extra retains **Ragas 0.2.6**, the compatible
**LangChain community 0.3** adapter and **datasets >=5.0.1**. Ragas 0.4 conflicts with the
CLI Rich 15 baseline; datasets 5.1 conflicts with the Hugging Face integration's
`hf-xet<1.5` constraint. Preserve the complete graph when locking an application;
see the [hotfix dependency notes](../../../dependency-updates.md). A future major upgrade needs separate adapter qualification.
