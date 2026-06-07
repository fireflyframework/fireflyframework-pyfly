<span class="eyebrow">Chapter 18</span>

# Extending PyFly & Going to Production {.chtitle}

::: figure art/openers/ch18.svg | &nbsp;

Lumen is no longer a toy. Over the past seventeen chapters you built
a wallet service from a single annotated class to a full event-driven
microservice with CQRS, sagas, EDA, HTTP clients, caching,
resilience, observability, security, and a test suite backed by
real containers. You know how to run it, debug it, and deploy it.

This final chapter is about the space between "it works on my
laptop" and "it runs reliably for real users." That space has three
concerns. First: extensibility — the ability to add behaviour without
forking the framework. Second: the missing features your domain
might need right now (a rules engine, centralised config, multiple
languages, live updates, a CLI). Third: the operational habits that
separate a weekend project from a production service.

You will move quickly. Every section picks one topic, shows the
minimal working API, and connects it to Lumen. By the end you will
have a complete picture of the PyFly ecosystem and a production
checklist you can tape to your monitor.

---

## Plugins and extension points

### Why a plugin system?

The core framework is intentionally small. Features that are optional
— formatters, audit sinks, notification channels — should be
pluggable so that teams can compose only what they need and ship their
own additions without touching framework internals.

PyFly's `pyfly.plugins` module mirrors Spring's plugin registry: you
declare an **extension point** (a named slot), **extensions**
(concrete contributions), and bundle them into a **plugin** with a
lifecycle.

::: listing lumen/plugins/audit.py | Listing 18.1 — Declaring an audit-sink plugin
from pyfly.plugins import (
    PluginManager,
    extension,
    extension_point,
    plugin,
)


@extension_point(id="audit-sinks")
class _AuditSink: ...


@plugin(id="console-audit", version="1.0.0")
class ConsoleAuditPlugin:

    @extension(point="audit-sinks", priority=10)
    class ConsoleSink:
        name = "console"

        def record(self, event: dict) -> None:
            print(f"[AUDIT] {event}")

    async def start(self) -> None:
        print("ConsoleAuditPlugin started")

    async def stop(self) -> None:
        print("ConsoleAuditPlugin stopped")
:::

**How it works.**

1. `@extension_point(id="audit-sinks")` registers a named slot so
   that the registry knows its interface type.
2. `@plugin(id="console-audit", version="1.0.0")` declares a
   plugin class with a mandatory `id` and `version`.
3. `@extension(point="audit-sinks", priority=10)` marks an inner
   class as a contribution to that slot. Higher priority wins first
   position when you iterate the results.

Loading and running the plugin:

::: listing lumen/plugins/runner.py | Listing 18.2 — Driving the plugin lifecycle
import asyncio

from pyfly.plugins import PluginManager

from lumen.plugins.audit import ConsoleAuditPlugin


async def main() -> None:
    manager = PluginManager()
    await manager.add(ConsoleAuditPlugin)
    await manager.start_all()

    sinks = await manager.registry.get("audit-sinks")
    for sink in sinks:
        sink.record({"action": "deposit", "amount": 100})

    await manager.stop_all()


asyncio.run(main())
:::

`PluginManager.add()` inspects the class for nested
`@extension_point` declarations first, then registers each
`@extension` contribution. `start_all()` invokes each plugin's
`init` then `start` hooks in dependency order; `stop_all()` reverses
the sequence, calling `stop` then `unload`. Circular dependencies
raise `PluginResolutionError` before any code runs.

| Method | Description |
|---|---|
| `await manager.add(cls)` | Scan and register a plugin class |
| `await manager.start_all()` | `init` → `start` in dependency order |
| `await manager.stop_all()` | `stop` → `unload` in reverse order |
| `await manager.remove(plugin_id)` | Unload one plugin; returns `False` if unknown |
| `await manager.registry.get(point_id)` | Extensions for a slot, priority-sorted |

!!! spring "Spring parity"
    `@plugin` / `@extension_point` / `@extension` mirror
    `@Plugin` / `ExtensionPoint` / `@Extension` from Spring's plugin
    API. `PluginManager.start_all()` plays the role of the Spring
    plugin container's lifecycle management. The dependency-order
    boot and reverse-order shutdown are identical in semantics.

---

## Business rules with the Rule Engine

Most real-world services have logic that belongs to the business, not
the code: "flag orders over $5,000", "block shipments to sanctioned
regions", "apply a 10% surcharge after hours." Hard-coding those
thresholds in Python means a rebuild every time the business changes
its mind.

PyFly's `pyfly.rule_engine` gives product owners a YAML dial they
can turn without touching source code.

### Defining rules in YAML

::: listing lumen/rules/transaction_rules.yaml | Listing 18.3 — Fraud and daily-limit rules
id: transaction-rules
name: Lumen transaction rules

rules:
  - id: daily-limit
    priority: 20
    when:
      op: ge
      field: transaction.daily_total
      value: 5000
    then:
      - type: set
        target: flags.limit_exceeded
        value: true
      - type: log
        value: "daily limit exceeded"

  - id: fraud-country
    priority: 10
    when:
      op: in
      field: transaction.country
      value: ["XX", "YY", "ZZ"]
    then:
      - type: set
        target: flags.fraud_risk
        value: true
      - type: log
        value: "high-risk country detected"

  - id: high-value
    priority: 5
    when:
      op: ge
      field: transaction.amount
      value: 1000
    then:
      - type: set
        target: flags.high_value
        value: true
:::

Each rule has a `when` condition and a list of `then` actions.
Conditions use these operators:

| Comparison | Logical |
|---|---|
| `eq`, `ne`, `gt`, `ge`, `lt`, `le` | `and`, `or`, `not` |
| `in`, `not_in`, `regex` | (with `conditions: [...]`) |

Actions are `set` (write a context path), `increment`, or `log`.
Subclass `RuleEvaluator._execute_action` to add `call`, `calculate`,
or any custom verb.

### Evaluating rules in a service

::: listing lumen/rules/risk_service.py | Listing 18.4 — Evaluating rules against a transaction
from pathlib import Path

from pyfly.container import service
from pyfly.rule_engine import RuleSetEvaluator, RuleSetLoader


@service
class RiskService:
    """Evaluate transaction-level rules and return risk flags."""

    def __init__(self) -> None:
        yaml_text = (
            Path(__file__).parent / "transaction_rules.yaml"
        ).read_text()
        self._ruleset = RuleSetLoader.from_yaml(yaml_text)
        self._evaluator = RuleSetEvaluator()

    def assess(
        self,
        amount: float,
        daily_total: float,
        country: str,
    ) -> dict:
        ctx = {
            "transaction": {
                "amount": amount,
                "daily_total": daily_total,
                "country": country,
            },
            "flags": {},
        }
        self._evaluator.evaluate(self._ruleset, ctx)
        return ctx["flags"]
:::

`RuleSetLoader.from_yaml(text)` parses the YAML into an AST.
`RuleSetEvaluator.evaluate(ruleset, ctx)` walks every rule in
priority order, evaluates the `when` clause, and applies matching
`then` actions by mutating `ctx` in place. The flags dict is the
authoritative output — a downstream handler can reject, queue, or
flag the transaction based on whatever keys are set.

::: figure art/figures/18-production.svg | Figure 18.1 — Rule evaluation at the service boundary. YAML rules are parsed once at startup; each transaction passes through the evaluator as a mutable context dict.

!!! tip "Hot-reload rules without redeployment"
    Store `transaction_rules.yaml` in the Config Server (see the
    next section) and re-parse on every fetch. Your rules engine
    becomes a live dial the business controls.

---

## Centralised config (Config Server)

As Lumen grows to multiple services, each one carries its own copy
of database URLs, timeouts, and feature flags. The Config Server
module removes that duplication: one service owns the truth;
everyone else fetches on startup.

### Running the server

Enable the server in `pyfly.yaml`:

```yaml
pyfly:
  config-server:
    enabled: true
    backend:
      root: /etc/lumen/config
```

That is all. PyFly auto-configures a `ConfigServer` backed by a
`FilesystemConfigBackend` and mounts HTTP routes automatically:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/{app}/{profile}` | Fetch merged config bundle |
| `GET` | `/{app}/{profile}/{label}` | Fetch for a specific label |
| `POST` | `/{app}/{profile}` | Save a config bundle |
| `GET` | `/_list` | List all stored bundles |

The response shape is Spring-Cloud-Config-compatible, so an existing
Spring service can consume the same endpoint without changes.

### Saving and fetching programmatically

::: listing lumen/config/seed.py | Listing 18.5 — Seeding and reading a config bundle
import asyncio

from pyfly.config_server import (
    ConfigClient,
    ConfigServer,
    FilesystemConfigBackend,
)


async def seed() -> None:
    server = ConfigServer(FilesystemConfigBackend("/etc/lumen/config"))
    await server.save(
        "wallet",
        "prod",
        {
            "db.url": "postgres://db:5432/lumen",
            "cache.ttl": 30,
        },
    )
    bundle = await server.fetch("wallet", "prod")
    print(bundle)


asyncio.run(seed())
:::

Client services bootstrap with:

::: listing lumen/config/bootstrap.py | Listing 18.6 — Fetching remote config at startup
from pyfly.config_server import ConfigClient


async def load_remote() -> dict:
    client = ConfigClient(
        url="http://config:8888",
        application="wallet",
        profile="prod",
        label="main",
    )
    return await client.fetch()
:::

`ConfigClient.fetch()` GETs
`{url}/{application}/{profile}/{label}`, merges the
`propertySources` array (highest priority first), and returns a flat
`{dotted_key: value}` dict. In normal operation you never call
`ConfigClient` directly — set `pyfly.cloud.config.uri` in
`pyfly.yaml` and `PyFlyApplication` calls it automatically during
bootstrap, merging the result into the application `Config` as a
high-precedence source.

!!! note "Fallback priority"
    The server assembles up to four overlay layers:
    `{app}/{profile}`, `{app}/default`, `application/{profile}`,
    `application/default`. A client merges them with the first
    source winning, so environment-specific overrides always beat
    shared defaults.

---

## Internationalisation (i18n)

Lumen's error messages and notifications currently live as Python
string literals. When users speak different languages that approach
does not scale.

Enable the i18n subsystem with a single flag:

```yaml
pyfly:
  i18n:
    enabled: true
    base-path: i18n/
    default-locale: en
```

### Writing resource bundles

```yaml
# i18n/messages_en.yaml
wallet:
  deposit_ok: "Deposited {0} to wallet {1}."
  limit_exceeded: "Daily limit exceeded. Maximum is {0}."

# i18n/messages_es.yaml
wallet:
  deposit_ok: "Se depositaron {0} en la billetera {1}."
  limit_exceeded: "Se superó el límite diario. El máximo es {0}."
```

`ResourceBundleMessageSource` resolves keys with dot notation and
substitutes `{n}` placeholders (zero-based) following
`MessageFormat` semantics. Missing codes fall back to
`default-locale`; if they are absent there too, `get_message` raises
`KeyError`.

### Injecting MessageSource into a service

::: listing lumen/i18n/notification_service.py | Listing 18.7 — Locale-aware notification service
from pyfly.container import service
from pyfly.i18n import AcceptHeaderLocaleResolver, MessageSource


@service
class NotificationService:
    """Renders user-facing messages in the caller's preferred locale."""

    def __init__(
        self,
        messages: MessageSource,
        locale_resolver: AcceptHeaderLocaleResolver,
    ) -> None:
        self._messages = messages
        self._resolver = locale_resolver

    def deposit_confirmation(
        self,
        request,
        amount: float,
        wallet_id: str,
    ) -> str:
        locale = self._resolver.resolve_locale(request)
        return self._messages.get_message_or_default(
            "wallet.deposit_ok",
            default="Deposit successful.",
            args=(amount, wallet_id),
            locale=locale,
        )
:::

`AcceptHeaderLocaleResolver` parses the `Accept-Language` header and
returns the highest-`q` primary subtag. Override with
`FixedLocaleResolver` for single-language deployments or tests.

!!! spring "Spring parity"
    `MessageSource`, `ResourceBundleMessageSource`,
    `AcceptHeaderLocaleResolver`, and `FixedLocaleResolver` are
    direct name equivalents of the Spring MVC i18n stack. The API
    differs only in the use of positional `{n}` placeholders instead
    of SpEL inside message strings.

---

## Real-time updates with WebSocket

The Lumen admin dashboard currently polls for balance changes. A
WebSocket endpoint eliminates the poll: the server pushes an update
the instant a deposit commits.

::: listing lumen/web/balance_ws_controller.py | Listing 18.8 — Live balance feed via WebSocket
import asyncio

from pyfly.container import rest_controller
from pyfly.web import request_mapping
from pyfly.websocket import WebSocketSession, websocket_mapping


@rest_controller
@request_mapping("/ws")
class BalanceFeedController:
    """Streams balance updates to connected clients."""

    def __init__(self, wallet_service) -> None:
        self._wallet = wallet_service
        self._clients: set[WebSocketSession] = set()

    @websocket_mapping("/balance/{wallet_id}")
    async def balance_feed(self, session: WebSocketSession) -> None:
        wallet_id = session.path_params["wallet_id"]
        await session.accept()
        self._clients.add(session)
        try:
            while True:
                balance = await self._wallet.get_balance(wallet_id)
                await session.send_json(
                    {"wallet_id": wallet_id, "balance": balance}
                )
                await asyncio.sleep(1)
        finally:
            self._clients.discard(session)

    async def on_disconnect(self, session: WebSocketSession) -> None:
        self._clients.discard(session)
:::

`@websocket_mapping("/balance/{wallet_id}")` mounts the endpoint at
`ws://<host>/ws/balance/{wallet_id}`. The full path is the
controller's `@request_mapping` base (`/ws`) concatenated with the
decorator's path.

`WebSocketSession` exposes the connection lifecycle:

| Method | Description |
|---|---|
| `await accept(subprotocol=None)` | Complete the handshake |
| `await send_json(data)` | Serialise and send a JSON message |
| `await send_text(data)` | Send a plain string |
| `await receive_text()` | Block until a text message arrives |
| `await receive_json()` | Block until a JSON message arrives |
| `await close(code=1000)` | Close the connection cleanly |

`session.path_params`, `session.query_params`, and
`session.headers` expose connection metadata. WebSocket routes are
auto-discovered alongside HTTP routes — no extra configuration is
needed.

The `on_disconnect` hook is invoked in a `finally` block after the
handler returns or the client disconnects, giving controllers a safe
place to release resources. A `WebSocketDisconnect` raised by the
client is treated as a normal close and does not propagate.

!!! tip "Broadcasting"
    Keep a `set[WebSocketSession]` on the controller and fan out
    with `for client in list(self._clients): await client.send_json(payload)`.
    Because controller beans are singletons the set lives for the
    lifetime of the application.

---

## Shell commands and startup runners

Not every feature of Lumen lives behind an HTTP endpoint. Database
seed scripts, one-time data migrations, and scheduled batch jobs are
better expressed as CLI commands that live inside the same DI
container — sharing services, configuration, and repositories with
the main application.

### @shell_component and @shell_method

::: listing lumen/cli/wallet_commands.py | Listing 18.9 — DI-powered shell commands
from pyfly.shell import (
    shell_argument,
    shell_component,
    shell_method,
    shell_option,
)


@shell_component
class WalletCommands:
    """Operational commands for the Lumen wallet service."""

    def __init__(self, wallet_service) -> None:
        self._wallet = wallet_service

    @shell_method(group="wallet", help="Deposit funds into a wallet")
    @shell_argument("wallet_id", help="Target wallet identifier")
    @shell_option("--amount", help="Amount to deposit (integer cents)")
    async def deposit(
        self, wallet_id: str, amount: int = 100
    ) -> str:
        result = await self._wallet.deposit(wallet_id, amount)
        return f"New balance: {result['balance']}"

    @shell_method(group="wallet", help="Show current balance")
    @shell_argument("wallet_id", help="Wallet to inspect")
    async def balance(self, wallet_id: str) -> str:
        data = await self._wallet.get_balance(wallet_id)
        return f"{wallet_id}: {data['balance']}"
:::

Enable the shell in `pyfly.yaml`:

```yaml
pyfly:
  shell:
    enabled: true
```

PyFly auto-configures a `ClickShellAdapter` and wires every
`@shell_method` on startup. The group name becomes a sub-command:

```bash
python -m lumen wallet deposit w-001 --amount 500
python -m lumen wallet balance w-001
python -m lumen --interactive  # REPL mode
```

### CommandLineRunner — one-shot post-startup tasks

For tasks that run once at startup (seeding, warm-up, connection
checks), implement `CommandLineRunner`:

::: listing lumen/runners/seed_runner.py | Listing 18.10 — Post-startup database seeder
from pyfly.container import service


@service
class SeedRunner:
    """Seed the database with a default admin wallet on first boot."""

    def __init__(self, wallet_service) -> None:
        self._wallet = wallet_service

    async def run(self, args: list[str]) -> None:
        if "--seed" in args:
            await self._wallet.ensure_default_wallet()
            print("Default wallet ensured.")
:::

Any bean with `async def run(self, args: list[str]) -> None`
structurally satisfies `CommandLineRunner`. The framework detects it
via `isinstance()` after `ApplicationReadyEvent` fires and invokes
it with the raw CLI arguments. Use `@order(n)` to control execution
order when multiple runners coexist.

!!! spring "Spring parity"
    `@shell_component`, `@shell_method`, `@shell_option`,
    `@shell_argument`, and `CommandLineRunner` are direct
    equivalents of Spring Shell's `@ShellComponent`,
    `@ShellMethod`, `@ShellOption`, `@ShellArgument`, and
    Spring Boot's `CommandLineRunner` interface. Click replaces
    JLine as the terminal library, but the programming model is
    identical.

---

## Generating an SDK from the OpenAPI spec

When Lumen exposes an HTTP API, downstream services should call it
via a generated client — not hand-written `httpx` calls that drift
out of sync. PyFly builds and serves an OpenAPI 3.1 spec
automatically at `/openapi.json`.

The spec is assembled by `OpenAPIGenerator` from the route metadata
collected by `ControllerRegistrar`:

- **Info** — populated from `title`, `version`, and `description`
  in `create_app()`.
- **Paths** — one operation per `@get_mapping` / `@post_mapping`
  etc., with parameters inferred from `PathVar[T]`, `QueryParam[T]`,
  `Header[T]`, and `Body[BaseModel]` type hints.
- **Schemas** — Pydantic models registered in `components.schemas`
  via `model_json_schema()` and referenced with `$ref`.

With the spec in hand, generate a Python client in one command using
the [OpenAPI Generator](https://openapi-generator.tech) tool:

```bash
# Download the spec from a running instance
curl http://localhost:8080/openapi.json -o lumen-spec.json

# Generate a Python client package
openapi-generator-cli generate \
  -i lumen-spec.json \
  -g python \
  -o lumen-client \
  --package-name lumen_client
```

The generated `lumen_client` package contains typed models and a
`DefaultApi` with one method per operation. Consumer services add it
as a dependency and call it without knowing anything about HTTP:

::: listing payment/services/wallet_client.py | Listing 18.11 — Consuming the generated Lumen SDK
from lumen_client import DefaultApi, ApiClient, Configuration


class WalletGateway:
    """Typed façade over the generated Lumen client SDK."""

    def __init__(self, base_url: str) -> None:
        cfg = Configuration(host=base_url)
        self._api = DefaultApi(ApiClient(cfg))

    def get_balance(self, wallet_id: str) -> float:
        result = self._api.get_wallet_balance(wallet_id)
        return result.balance
:::

!!! tip "Keep the spec versioned"
    Check `lumen-spec.json` into the Lumen repository and
    regenerate client packages in CI whenever the spec changes.
    Downstream teams pin to a specific spec version via their
    dependency manager — the same discipline Java teams use with
    Maven artifact versions.

---

## Going to production

### Packaging with Docker

`pyfly new` generates a `Dockerfile` for every archetype. For a web
service it looks like this after minor production hardening:

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install uv && \
    uv sync --no-dev --extra web --extra data-relational \
             --extra security --extra observability

COPY src/ src/
COPY pyfly.yaml .

EXPOSE 8080
CMD ["pyfly", "run", "--host", "0.0.0.0", \
     "--port", "8080", "--server", "granian", "--workers", "2"]
```

Install only the extras your service actually needs — the `full`
meta-extra pulls in Kafka, RabbitMQ, and MongoDB drivers even when
you use none of them.

### Environment variables and secrets

Never bake secrets into `pyfly.yaml`. PyFly resolves `${ENV_VAR}`
placeholders anywhere in configuration, so a `pyfly.yaml` fragment
like:

```yaml
pyfly:
  data:
    relational:
      url: ${DATABASE_URL}

  security:
    jwt:
      secret: ${JWT_SECRET}
```

reads the actual values from the container environment at startup.
In Kubernetes, back those variables with a `Secret`; in Docker
Compose, use an `.env` file that is never committed. The
`pyfly doctor` command checks that required tools are present, but
does not validate secrets — that is your responsibility.

### Graceful shutdown

PyFly honours `server.shutdown: graceful` by default. Set
`pyfly.server.graceful-timeout` (seconds) to control how long the
server waits for in-flight requests to complete before forcing exit:

```yaml
pyfly:
  server:
    graceful-timeout: 30
```

SIGTERM triggers the shutdown sequence: the server stops accepting
new connections, the `ApplicationContext.stop()` runs
`@pre_destroy` hooks and `stop_all()` for plugins, and the process
exits cleanly. In Kubernetes set `terminationGracePeriodSeconds` to
at least five seconds more than `graceful-timeout`.

### Server selection

The ASGI server is selected by priority at runtime:

| Priority | Server | Install extra |
|---|---|---|
| 1 | Granian (Rust/tokio) | `granian` |
| 2 | Uvicorn | `web` (default) |
| 3 | Hypercorn | `hypercorn` |

For production, prefer Granian — it delivers roughly 3× the
throughput of Uvicorn with native HTTP/2. Pair it with uvloop on
Linux for an additional 2–4× event-loop speedup:

```bash
uv add "pyfly[web-fast]"   # granian + uvloop in one shot
```

Force the choice in YAML to avoid surprises on servers where
multiple servers happen to be installed:

```yaml
pyfly:
  server:
    type: granian
    event-loop: uvloop
    workers: 4
    graceful-timeout: 30
```

### Health endpoints

PyFly exposes Spring-Boot-style actuator endpoints out of the box:

| Endpoint | Purpose |
|---|---|
| `GET /actuator/health` | Aggregate health (UP / DOWN) |
| `GET /actuator/health/liveness` | Kubernetes liveness probe |
| `GET /actuator/health/readiness` | Kubernetes readiness probe |
| `GET /actuator/metrics` | Prometheus-compatible metrics |

Add them to `pyfly.yaml`:

```yaml
management:
  endpoints:
    web:
      exposure:
        include: health,metrics
  endpoint:
    health:
      show-details: when-authorized
```

Then wire your Kubernetes deployment:

```yaml
livenessProbe:
  httpGet:
    path: /actuator/health/liveness
    port: 8080
  initialDelaySeconds: 10
  periodSeconds: 15
readinessProbe:
  httpGet:
    path: /actuator/health/readiness
    port: 8080
  initialDelaySeconds: 5
  periodSeconds: 10
```

### The production checklist

- [ ] All secrets are environment variables — none are in `pyfly.yaml`
      or source control.
- [ ] Docker image installs only the extras the service uses.
- [ ] Server is pinned to Granian + uvloop; `workers` is set
      explicitly (not left at `1` for multi-core machines).
- [ ] `graceful-timeout` is at least 15 s; Kubernetes
      `terminationGracePeriodSeconds` is at least 5 s more.
- [ ] Liveness and readiness probes are configured and tested.
- [ ] `/actuator/health` returns UP before traffic is sent.
- [ ] Prometheus metrics endpoint is scraped by the monitoring stack.
- [ ] Structured logging is enabled (`pyfly[observability]`) and
      log level is `INFO` in production, not `DEBUG`.
- [ ] OpenTelemetry exporter is pointed at the production collector.
- [ ] Database migrations (`pyfly db upgrade`) run in a pre-deploy
      step, not at application startup.
- [ ] The generated OpenAPI spec is versioned in CI and downstream
      SDK packages pin to a specific spec revision.
- [ ] `pyfly doctor` passes on every developer machine and CI runner.

---

## What you built {.recap}

Seventeen chapters ago you typed `@pyfly_application` and watched
the DI container wire your first `@service`. Today that same
container powers a production wallet platform.

Here is what you constructed along the way.

**Chapters 1–3** gave you the foundation: a first-class DI
container, flexible configuration (YAML, env, profiles, SpEL
expressions), and a complete HTTP layer with request mapping,
filters, content negotiation, and a JSON serialization layer you can
replace without touching a single controller.

**Chapters 4–6** introduced persistence. You mapped entities with
SQLAlchemy, managed schema evolution with Alembic, and structured
your domain with DDD tactical patterns — aggregates, value objects,
and repositories that keep business logic out of infrastructure.

**Chapters 7–9** brought the architecture alive. CQRS split reads
from writes at the handler level. EDA let services react to events
without polling. Event sourcing made every state change a first-class
fact — replayable, auditable, and the foundation for projections.

**Chapters 10–12** taught Lumen to leave its own process. Resilient
HTTP clients called downstream services without cascading failures.
Sagas orchestrated multi-step transactions across service boundaries
with automatic compensation when any step went wrong.

**Chapters 13–15** hardened the platform. Caching cut database
pressure. Rate limiters, bulkheads, timeouts, and circuit breakers
turned every dependency into a controlled blast radius. Distributed
tracing, structured logging, and a live admin dashboard gave you eyes
inside the system.

**Chapters 16–17** closed the feedback loop. A structured test suite
— unit, integration, and Testcontainers-backed contract tests — made
the platform safe to change. Scheduled tasks, push notifications,
webhooks, and callbacks let Lumen reach out to the world on its own
schedule.

**Chapter 18** showed you what lies beyond the core: a plugin system
for open extension, a YAML rule engine for business-owned logic, a
Config Server for fleet-wide configuration, i18n for global
audiences, WebSocket for real-time UX, a Shell module for
operational tooling, an OpenAPI spec that generates typed client SDKs
for free, and the production habits that keep all of it running.

PyFly is not magic. Every abstraction in this book has a cost, and
you now understand what that cost is: a DI container that starts in
milliseconds but requires you to think about bean scopes; a reactive
HTTP server that handles thousands of concurrent connections but
requires you to avoid blocking; a saga engine that survives partial
failures but requires you to write compensating transactions.

Understanding the cost is what separates a practitioner from a
copier. You are now a practitioner.

---

## Try it yourself {.exercises}

1. **Add a custom plugin.** Implement an `AuditPlugin` that
   contributes an extension to an `"audit-sinks"` extension point.
   The extension should write each event to a file.
   In a test, use `PluginManager.registry.get("audit-sinks")` to
   assert that your extension is returned with the expected `name`.

2. **Ship a rules change without redeployment.** Store
   `transaction_rules.yaml` in the Config Server
   (`pyfly.config-server.enabled: true`). Write a `RiskService`
   that fetches the YAML from a `ConfigClient` on each call to
   `assess()` (or caches it with a short TTL). Update the `value`
   threshold in the YAML through the `POST /{app}/{profile}` route
   and verify that `assess()` picks up the new threshold without a
   restart.

3. **Localise a rejection message.** Add `wallet.limit_exceeded`
   to `i18n/messages_en.yaml` and `i18n/messages_es.yaml`. Wire a
   `NotificationService` that reads the locale from the
   `Accept-Language` header and returns the correct string. Write
   two tests — one with `Accept-Language: en`, one with
   `Accept-Language: es` — and assert the correct message is
   returned for each.

---

Lumen is ready for production. For what comes next — new modules,
community plugins, and release notes — visit the framework
documentation at
[github.com/fireflyframework/fireflyframework-pyfly](https://github.com/fireflyframework/fireflyframework-pyfly).
Every concept in this book lives in that repository; the source is
the ultimate reference.
