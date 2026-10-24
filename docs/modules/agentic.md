# Agentic

The opt-in `pyfly.agentic` module integrates agents, tools, model routing,
conversations, content processing, embeddings, vector stores, reasoning, validation,
pipelines, dynamic workflows and operational controls with PyFly's configuration,
dependency injection and application lifecycle. It preserves the source capability
APIs beneath the new namespace; Pydantic AI remains the provider execution engine.

Install the `agentic` extra from the staged PyFly v26.10.08 candidate or a verified
later release, then set `pyfly.agentic.enabled`
to `true`. The module is disabled by default; PyFly's base installation does not
need the AI SDKs. See [installation and every extra](agentic/getting-started/installation.md).

```yaml
pyfly:
  agentic:
    enabled: true
    default_model: openai-responses:gpt-6-luna
```

Each application owns an `AgenticRuntime`, configuration, registries, usage/quota
state and managed resources. Inject `AgentFactory` into services and call
`factory.create(name, **kwargs)`; use an explicit runtime for scripts. The
[native DI and lifecycle tutorial](agentic/native-runtime.md) contains complete
offline and live-service examples and explains shutdown and ownership.

| Task | Guide |
| --- | --- |
| First agent, tool and conversation | [Quick start](agentic/getting-started/quickstart.md) |
| Complete capability reference | [Guide catalogue](agentic/catalogue.md) |
| Model/API options | [Models](agentic/models.md) |
| Retrieval and scope | [Content](agentic/content.md), [embeddings](agentic/embeddings.md), [vector stores](agentic/vectorstores.md) |
| Multi-step execution | [Reasoning](agentic/reasoning.md), [pipelines](agentic/pipeline.md), [workflows](agentic/workflows.md) |
| Operating controls | [Observability](agentic/observability.md), [security](agentic/security.md), [storage](agentic/storage.md) |
| Runnable examples and requirements | [Examples](agentic/examples.md) |
| Existing standalone applications | [Migration](agentic/migration.md) |

Application-scoped state does not supply authentication or database RLS. Runtime
budgets depend on priced usage; tools still need idempotency for external effects.
Persistent working memory and checkpoint journals have their documented scope and
recovery semantics. The [visual guide](agentic/diagrams.md) makes these boundaries explicit.

Native database memory and pgvector borrow PyFly Data resources and join its unit
of work. Async services await memory APIs; obsolete URL/pool controls require
migration. Content encryption is applied during default composition, while
custom managers own their confidentiality policy. The existing
[admin console](admin.md#agentic-application-runtime-only) adds guarded read-only Agentic inventory and
retained accounting. See [native cutover](agentic/migration.md#native-data-and-runtime-cutover).
