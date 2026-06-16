<span class="eyebrow">Chapter 18</span>

# Extending PyFly & Going to Production {.chtitle}

::: figure art/openers/ch18.svg | &nbsp;

Lumen is no longer a toy. Over the past seventeen chapters you built a wallet service from a single annotated class to a full event-driven microservice — CQRS, sagas, EDA, HTTP clients, caching, resilience, observability, security, and a test suite backed by real containers. You know how to run it, debug it, and deploy it.

This final chapter is about the distance between "it works on my laptop" and "it runs reliably for real users." That distance has three dimensions. First: **extensibility** — the ability to add behaviour without forking the framework. Second: the features your domain may need right now — a rules engine, centralised config, multiple languages, real-time push, a CLI. Third: the operational habits that separate a weekend project from a production service.

You will move quickly. Every section picks one topic, shows the minimal working API, and connects it to Lumen. By the end you will have a complete picture of the PyFly ecosystem and a production checklist worth keeping close.

!!! note "Conventions in this chapter"
    The listings and config keys here target PyFly **v26.6.110**. Two
    deployment facts from that release run through the whole "Going to
    production" section, so it helps to know them up front:

    - The application listens on `pyfly.server.port` (default `8080`), the
      Spring `server.port` parity key. The old `pyfly.web.port` /
      `PYFLY_WEB_PORT` keys were removed in v26.6.102.
    - The Actuator and Admin Dashboard run on a **separate management port**
      (`pyfly.management.server.port`, default `9090`), open and
      unauthenticated by default. We cover this in detail when we wire up
      health probes.

    Each feature below is built the same way: a short "why", the code, then a
    **Run it** checkpoint that shows the exact command and the output you
    should see. Type the commands as you go — that is how the ideas stick.

---

## Plugins and extension points

### Why a plugin system?

The core framework is intentionally small. Optional features — formatters, audit sinks, notification channels — should be pluggable so teams can compose only what they need and ship additions without touching framework internals.

PyFly's `pyfly.plugins` module mirrors Spring's plugin registry: you declare an **extension point** (a named slot), **extensions** (concrete contributions), and bundle them into a **plugin** with a lifecycle.

New jargon, in plain terms: an **extension point** is a labelled hole in your application — "anything that wants to be an audit sink plugs in here." An **extension** is one thing that fills the hole. A **plugin** is the package that ships one or more extensions together and can be started and stopped as a unit. If you have ever defined a Java interface and let callers register implementations of it, you already know the shape — the decorators below just make the wiring declarative.

We will build the smallest useful example: a console audit sink that prints every event it is handed.

**Step 1 — Declare the extension point.** This is the contract. Every audit sink must promise a `record(event)` method. The `id="audit-sinks"` string is the name other code uses to find every sink later.

**Step 2 — Write a plugin that contributes one sink.** The `@plugin` decorator wraps a class with a mandatory `id` and `version`; the inner `@extension` class is the actual contribution. It inherits the extension-point interface (`AuditSink`) so the registry can confirm it honours the contract.

**Step 3 — Give the plugin a lifecycle.** The `start` and `stop` methods are where you open and close resources (a file handle, a network connection). The plugin manager calls them for you.

Here are all three steps in one file.

::: listing lumen/plugins/audit.py | Listing 18.1 — Declaring an audit-sink plugin
from pyfly.plugins import (
    PluginManager,
    extension,
    extension_point,
    plugin,
)


@extension_point(id="audit-sinks")
class AuditSink:
    """Interface that all audit sinks must implement."""

    def record(self, event: dict) -> None: ...


@plugin(id="console-audit", version="1.0.0")
class ConsoleAuditPlugin:

    @extension(point="audit-sinks", priority=10)
    class ConsoleSink(AuditSink):
        name = "console"

        def record(self, event: dict) -> None:
            print(f"[AUDIT] {event}")

    async def start(self) -> None:
        print("ConsoleAuditPlugin started")

    async def stop(self) -> None:
        print("ConsoleAuditPlugin stopped")
:::

**How it works.** `@extension_point(id="audit-sinks")` registers a named slot and declares the interface every contribution must implement. `@plugin(id="console-audit", version="1.0.0")` declares a plugin class with a mandatory `id` and `version`. `@extension(point="audit-sinks", priority=10)` marks an inner class as a contribution to that slot; the inner class must inherit the extension-point interface so the registry can validate it. Higher priority wins first position when iterating results.

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
        sink.record({"action": "deposit", "amount_minor": 100})

    await manager.stop_all()


asyncio.run(main())
:::

`PluginManager.add()` inspects the class for nested `@extension_point` declarations, then registers each `@extension` contribution. `start_all()` invokes each plugin's `init` then `start` hooks in dependency order; `stop_all()` reverses the sequence, calling `stop` then `unload`. Circular dependencies raise `PluginResolutionError` before any code runs.

!!! tip "Run it"
    Save both listings under `src/lumen/plugins/` and run the runner module
    directly:

    ```bash
    uv run python -m lumen.plugins.runner
    ```

    You should see the lifecycle hooks fire, the single event get recorded,
    and the clean shutdown:

    ```
    ConsoleAuditPlugin started
    [AUDIT] {'action': 'deposit', 'amount_minor': 100}
    ConsoleAuditPlugin stopped
    ```

    The order is the whole point: `start` runs before any sink is used, your
    code iterates the registered sinks, and `stop` runs last. If you add a
    second plugin later, `start_all()` boots both in dependency order and
    `stop_all()` shuts them down in reverse — you never manage that ordering
    by hand.

**What just happened.** You added a capability to the application — audit
recording — without editing a single line of framework code. The framework
discovered your plugin, validated that its extension honours the
`AuditSink` contract, ran its lifecycle, and handed you the registered sinks
to call. That is the core promise of a plugin system: open for extension,
closed for modification.

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

Most real-world services carry logic that belongs to the business, not the code: "flag orders over 500,000 cents", "block shipments to sanctioned regions", "apply a surcharge after hours." Hard-coding those thresholds in Python means a rebuild every time the business changes its mind.

PyFly's `pyfly.rule_engine` gives product owners a YAML dial they can turn without touching source code.

A **rule engine**, in plain terms, is a small interpreter for "if this, then that" statements that live in data rather than code. You feed it a bag of facts (the *context*) and a set of rules; it checks each rule's condition against the facts and, for the ones that match, performs the listed actions — usually writing a flag back into the context. The win is that the *rules* are editable by non-programmers, while the *engine* that runs them is fixed and tested.

### Defining rules in YAML

We will build a fraud-and-limit check in two steps: first write the rules as data, then wire a service that runs them.

**Step 1 — Write the rules as YAML.** Each rule names a `when` condition and a list of `then` actions. Because the rules are plain data, a product owner can review them in a pull request and a config server can hot-swap them at runtime.

Rules live in a separate YAML file that any team member can review. The evaluator parses this file once at startup — or on each fetch if you hot-reload from a Config Server (see the next section).

::: listing lumen/rules/transaction_rules.yaml | Listing 18.3 — Fraud and daily-limit rules (amounts in minor units)
id: transaction-rules
name: Lumen transaction rules

rules:
  - id: daily-limit
    priority: 20
    when:
      op: ge
      field: transaction.daily_total
      value: 500000
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
      value: 100000
    then:
      - type: set
        target: flags.high_value
        value: true
:::

Each rule has a `when` condition and a list of `then` actions. Amounts are always **integer minor units** (cents), so `100000` is €1,000.00. Conditions use these operators:

| Comparison | Logical |
|---|---|
| `eq`, `ne`, `gt`, `ge`, `lt`, `le` | `and`, `or`, `not` |
| `in`, `not_in`, `regex` | (with `conditions: [...]`) |

Actions are `set` (write to a context path), `increment`, or `log`. Subclass `RuleEvaluator` and override `_execute_action` to add `call`, `calculate`, or any custom verb.

**Step 2 — Wire a service that loads and runs the rules.** The service parses the YAML once at construction, then exposes an `assess()` method that builds a context, runs the evaluator, and returns the flags the rules set.

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
        amount: int,
        daily_total: int,
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

**How it works.** `RuleSetLoader.from_yaml(text)` parses the YAML into an AST. `RuleSetEvaluator.evaluate(ruleset, ctx)` walks every rule in priority order, evaluates the `when` clause, and applies matching `then` actions — mutating `ctx` in place and returning a `list[EvaluationResult]`. The `flags` dict is the authoritative output: a downstream handler rejects, queues, or flags the transaction based on whatever keys are set. `amount` and `daily_total` are in minor units (cents) to match the Lumen domain.

!!! tip "Run it"
    Exercise the service from a Python REPL to see the rules fire. A
    €6,000.00 transfer (`600000` minor units) trips both the high-value and
    daily-limit thresholds:

    ```bash
    uv run python -c "
    from lumen.rules.risk_service import RiskService
    print(RiskService().assess(amount=600000, daily_total=600000, country='US'))
    "
    ```

    Expected output — the flags the matching rules set, and nothing else:

    ```python
    {'high_value': True, 'limit_exceeded': True}
    ```

    Now drop the amount to `50000` (€500.00) with a clean country and you get
    back an empty `{}` — no rule matched, so nothing was flagged. You changed
    the *outcome* without touching any Python: that is the dial the YAML gives
    your product owners.

::: figure art/figures/18-production.svg | Figure 18.1 — Rule evaluation at the service boundary. YAML rules are parsed once at startup; each transaction passes through the evaluator as a mutable context dict.

!!! tip "Hot-reload rules without redeployment"
    Store `transaction_rules.yaml` in the Config Server (see the
    next section) and re-parse on every fetch. Your rules engine
    becomes a live dial the business controls.

---

## Centralised config (Config Server)

As Lumen grows to multiple services, each carries its own copy of database URLs, timeouts, and feature flags. The Config Server module removes that duplication: one service owns the truth; every other service fetches on startup.

A **config server**, in plain terms, is a small HTTP service whose entire job is to hand out configuration. Instead of baking timeouts and URLs into each service's own `pyfly.yaml`, you store them in one place and every service asks for its bundle at boot. Change the value once, restart (or refresh) the consumers, and the whole fleet moves together.

### Running the server

Enable the server in `pyfly.yaml`:

```yaml
pyfly:
  config-server:
    enabled: true
    backend:
      root: /etc/lumen/config
```

That is all. PyFly auto-configures a `ConfigServer` backed by a `FilesystemConfigBackend` and mounts the HTTP routes automatically:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/{app}/{profile}` | Fetch merged config bundle |
| `GET` | `/{app}/{profile}/{label}` | Fetch for a specific label |
| `POST` | `/{app}/{profile}` | Save a config bundle |
| `GET` | `/_list` | List all stored bundles |

The response shape is Spring Cloud Config-compatible, so an existing Spring service can consume the same endpoint without changes.

### Saving and fetching config programmatically

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

!!! tip "Run it"
    With `pyfly.config-server.enabled: true` in `pyfly.yaml`, start the app
    and curl the bundle you seeded. Remember: the config-server routes are
    served on the **application** port (`8080`), not the management port.

    ```bash
    uv run pyfly run
    # in another terminal:
    curl http://localhost:8080/wallet/prod
    ```

    The response is Spring Cloud Config-shaped — your seeded keys arrive
    inside a `propertySources` array:

    ```json
    {
      "name": "wallet",
      "profiles": ["prod"],
      "propertySources": [
        {"name": "wallet-prod", "source": {"db.url": "postgres://db:5432/lumen", "cache.ttl": 30}}
      ]
    }
    ```

    Because the shape matches Spring Cloud Config, an existing Spring Boot
    service can point its `spring.config.import=configserver:` at this same
    URL and consume it unchanged.

Client services fetch on startup with:

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

`ConfigClient.fetch()` GETs `{url}/{application}/{profile}/{label}`, merges the `propertySources` array (highest priority first), and returns a flat `{dotted_key: value}` dict. In normal operation you never call `ConfigClient` directly — set `pyfly.cloud.config.uri` in `pyfly.yaml` and `PyFlyApplication` calls it automatically during bootstrap, merging the result into the application `Config` as a high-precedence source.

!!! note "Fallback priority"
    The server assembles up to four overlay layers:
    `{app}/{profile}`, `{app}/default`, `application/{profile}`,
    `application/default`. A client merges them with the first
    source winning, so environment-specific overrides always beat
    shared defaults.

---

## Internationalisation (i18n)

Lumen's error messages and notifications currently live as Python string literals. When users speak different languages, that approach does not scale.

**i18n** is shorthand for *internationalisation* (the 18 letters between the "i" and the "n"). In practice it means pulling every user-facing string out of your code into per-language **resource bundles**, then choosing the right bundle at runtime based on the caller's preferred language. Your code refers to a string by a stable key like `wallet.deposit_ok`; the framework looks up the translation.

Enable the i18n subsystem with a single flag:

```yaml
pyfly:
  i18n:
    enabled: true
    base-path: i18n/
    default-locale: en
```

### Writing resource bundles

**Step 1 — Write one bundle per language.** Files are named `messages_<locale>.yaml`. Keys are shared across languages; only the values change. The `{0}`, `{1}` markers are positional placeholders the framework fills in at render time.

```yaml
# i18n/messages_en.yaml
wallet:
  deposit_ok: "Deposited {0} minor units to wallet {1}."
  limit_exceeded: "Daily limit exceeded. Maximum is {0} minor units."

# i18n/messages_es.yaml
wallet:
  deposit_ok: "Se depositaron {0} unidades menores en la billetera {1}."
  limit_exceeded: "Se superó el límite diario. El máximo es {0} unidades menores."
```

**`ResourceBundleMessageSource`** resolves keys with dot notation and substitutes `{n}` placeholders (zero-based) following `MessageFormat` semantics. Missing codes fall back to `default-locale`; if they are absent there too, `get_message` raises `KeyError`.

### Using MessageSource in a service

**Step 2 — Inject `MessageSource` and resolve the locale from the request.** The service below reads the caller's `Accept-Language` header, picks the matching bundle, and renders the message with the runtime arguments.

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
        amount_minor: int,
        wallet_id: str,
    ) -> str:
        locale = self._resolver.resolve_locale(request)
        return self._messages.get_message_or_default(
            "wallet.deposit_ok",
            default="Deposit successful.",
            args=(amount_minor, wallet_id),
            locale=locale,
        )
:::

`AcceptHeaderLocaleResolver` parses the `Accept-Language` header and returns the highest-`q` primary subtag. Use `FixedLocaleResolver` for single-language deployments or tests. Auto-configuration registers both when `pyfly.i18n.enabled: true`; inject either `MessageSource` (the port protocol) or the concrete `ResourceBundleMessageSource` — both work.

!!! tip "Run it"
    The fastest way to prove the lookup works is a test that resolves the
    bundle directly — no HTTP server required. Save Listing 18.7a under
    `tests/`, then run just this test:

    ```bash
    uv run --extra dev pytest tests/test_messages.py -q
    ```

    Expected:

    ```
    1 passed in 0.05s
    ```

    Swap `locale="es"` for `locale="en"` and the assertion would need the
    English string instead — same key, different bundle. That is the whole
    point of i18n: your code never changes, only the resolved locale does.

::: listing tests/test_messages.py | Listing 18.7a — Asserting a translated message
from pyfly.i18n.adapters.resource_bundle import ResourceBundleMessageSource


def test_deposit_message_in_spanish() -> None:
    messages = ResourceBundleMessageSource(base_path="i18n/", default_locale="en")
    text = messages.get_message(
        "wallet.deposit_ok", args=(100, "w-001"), locale="es"
    )
    assert text == "Se depositaron 100 unidades menores en la billetera w-001."
:::

!!! spring "Spring parity"
    `MessageSource`, `ResourceBundleMessageSource`,
    `AcceptHeaderLocaleResolver`, and `FixedLocaleResolver` are
    direct name equivalents of the Spring MVC i18n stack. The API
    differs only in the use of positional `{n}` placeholders instead
    of SpEL inside message strings.

---

## Real-time updates with WebSocket

The Lumen admin dashboard currently polls for balance changes. A WebSocket endpoint eliminates the poll — the server pushes an update the instant a deposit commits.

A **WebSocket** is a two-way connection that stays open. A normal HTTP request is one-shot: the client asks, the server answers, the line closes. With a WebSocket the line stays up, so the *server* can send data whenever it likes — perfect for live updates. The URL scheme is `ws://` (or `wss://` over TLS) instead of `http://`.

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
                    {"wallet_id": wallet_id, "balance_minor": balance}
                )
                await asyncio.sleep(1)
        finally:
            self._clients.discard(session)

    async def on_disconnect(self, session: WebSocketSession) -> None:
        self._clients.discard(session)
:::

**How it works.** `@websocket_mapping("/balance/{wallet_id}")` mounts the endpoint at `ws://<host>/ws/balance/{wallet_id}`. The full path is the controller's `@request_mapping` base (`/ws`) concatenated with the decorator's path.

!!! tip "Run it"
    Start the app, then open the live feed with any WebSocket client. Using
    `websocat` (`brew install websocat`):

    ```bash
    uv run pyfly run
    # in another terminal:
    websocat ws://localhost:8080/ws/balance/w-001
    ```

    You should see a JSON frame arrive roughly once a second, pushed by the
    server without you asking again:

    ```json
    {"wallet_id": "w-001", "balance_minor": 5000}
    {"wallet_id": "w-001", "balance_minor": 5000}
    ```

    Deposit into `w-001` from another terminal and watch the `balance_minor`
    value jump on the next frame — no polling, no refresh. Press `Ctrl+C` to
    close; the controller's `on_disconnect` runs and the session is dropped
    from the broadcast set.

`WebSocketSession` exposes the connection lifecycle:

| Method | Description |
|---|---|
| `await accept(subprotocol=None)` | Complete the handshake |
| `await send_json(data)` | Serialise and send a JSON message |
| `await send_text(data)` | Send a plain string |
| `await receive_text()` | Block until a text message arrives |
| `await receive_json()` | Block until a JSON message arrives |
| `await close(code=1000, reason=None)` | Close the connection cleanly |

`session.path_params`, `session.query_params`, and `session.headers` expose connection metadata. WebSocket routes are auto-discovered alongside HTTP routes — no extra configuration required.

The optional `on_disconnect` method is invoked automatically by the registrar after the `@websocket_mapping` handler returns or the client closes the connection (only if the connection was accepted first), giving controllers a safe place to release resources.

!!! tip "Broadcasting"
    Keep a `set[WebSocketSession]` on the controller and fan out
    with `for client in list(self._clients): await client.send_json(payload)`.
    Because controller beans are singletons the set lives for the
    lifetime of the application.

---

## Shell commands and startup runners

Not every feature lives behind an HTTP endpoint. Database seed scripts, one-time data migrations, and scheduled batch jobs are better expressed as CLI commands that run inside the same DI container — sharing services, configuration, and repositories with the main application.

The key idea here: a **shell command** is just a method that runs from the terminal but still has full access to your application's wired services. You do not write a separate script that re-creates the database connection by hand — the command receives the same `WalletService` your HTTP controllers use, because it lives inside the same DI container.

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
    @shell_option("--amount", help="Amount in minor units (integer cents)")
    async def deposit(
        self, wallet_id: str, amount: int = 100
    ) -> str:
        result = await self._wallet.deposit(wallet_id, amount)
        return f"New balance: {result['balance_minor']} minor units"

    @shell_method(group="wallet", help="Show current balance")
    @shell_argument("wallet_id", help="Wallet to inspect")
    async def balance(self, wallet_id: str) -> str:
        data = await self._wallet.get_balance(wallet_id)
        return f"{wallet_id}: {data['balance_minor']} minor units"
:::

Enable the shell in `pyfly.yaml`:

```yaml
pyfly:
  shell:
    enabled: true
```

PyFly auto-configures a `ClickShellAdapter` and wires every `@shell_method` at startup. The group name becomes a sub-command:

```bash
python -m lumen wallet deposit w-001 --amount 500
python -m lumen wallet balance w-001
python -m lumen        # no args → drops into REPL mode
```

!!! tip "Run it"
    Deposit 500 minor units into a wallet straight from the command line —
    the command runs your real `WalletService` against the real database:

    ```bash
    uv run python -m lumen wallet deposit w-001 --amount 500
    ```

    Expected output (the return value of your `deposit` method, printed by
    the shell adapter):

    ```
    New balance: 500 minor units
    ```

    Run `uv run python -m lumen wallet balance w-001` and you will see the
    same `500` reflected back — proof that the command and the HTTP API are
    talking to the same persistent state, not a throwaway in-memory copy.

### CommandLineRunner — one-shot post-startup tasks

For tasks that run once at startup — seeding, warm-up, connection checks — implement **`CommandLineRunner`**:

::: listing lumen/runners/seed_runner.py | Listing 18.10 — Post-startup database seeder
from pyfly.container import service
from pyfly.shell import CommandLineRunner


@service
class SeedRunner(CommandLineRunner):
    """Seed the database with a default admin wallet on first boot."""

    def __init__(self, wallet_service) -> None:
        self._wallet = wallet_service

    async def run(self, args: list[str]) -> None:
        if "--seed" in args:
            await self._wallet.ensure_default_wallet()
            print("Default wallet ensured.")
:::

Any bean whose class implements `async def run(self, args: list[str]) -> None` structurally satisfies the `CommandLineRunner` protocol. The framework detects it via `isinstance()` (the protocol is `@runtime_checkable`) after `ApplicationReadyEvent` fires, then invokes it with the raw CLI arguments. Use `@order(n)` to control execution order when multiple runners coexist.

!!! tip "Run it"
    The runner fires during application startup and receives the process's
    `sys.argv[1:]`, so launch the app through its CLI entry point with the
    flag appended:

    ```bash
    uv run python -m lumen --seed
    ```

    After the banner and the route table, you will see the runner's
    confirmation line:

    ```
    Default wallet ensured.
    ```

    Boot without `--seed` and the line is absent — the `if "--seed" in args`
    guard skips the work. That is the difference between a *runner* (runs
    every boot, you gate it yourself) and a one-off shell command (runs only
    when you invoke its name).

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

When Lumen exposes an HTTP API, downstream services should call it via a generated client — not hand-written `httpx` calls that drift out of sync. PyFly builds and serves an OpenAPI 3.1 spec automatically at `/openapi.json`.

An **OpenAPI spec** is a machine-readable description of your HTTP API: every path, every parameter, every request and response shape. An **SDK** (software development kit) is the client library a tool generates *from* that spec — typed methods that wrap the HTTP calls for you. The chain is: your controllers → the spec → a generated client. Because each link is mechanical, the client can never silently drift out of sync with the server.

`OpenAPIGenerator` assembles the spec from route metadata collected by `ControllerRegistrar`:

- **Info** — populated from `title`, `version`, and `description` passed to `create_app()`.
- **Paths** — one operation per `@get_mapping` / `@post_mapping` etc., with parameters inferred from `PathVar[T]`, `QueryParam[T]`, `Header[T]`, and `Body[BaseModel]` type hints.
- **Schemas** — Pydantic models registered in `components.schemas` via `model_json_schema()` and referenced with `$ref`.

With the spec available, generate a Python client in two steps — download the spec, then run the generator.

**Step 1 — Download the spec from a running instance.** The spec lives on the application port (`8080`), alongside your business routes.

**Step 2 — Run the OpenAPI generator** to turn that JSON into an installable Python package.

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

The generated `lumen_client` package contains typed models and a `DefaultApi` with one method per operation. Consumer services add it as a dependency and call it without knowing anything about HTTP:

::: listing payment/services/wallet_client.py | Listing 18.11 — Consuming the generated Lumen SDK
from lumen_client import ApiClient, Configuration, DefaultApi


class WalletGateway:
    """Typed façade over the generated Lumen client SDK."""

    def __init__(self, base_url: str) -> None:
        cfg = Configuration(host=base_url)
        self._api = DefaultApi(ApiClient(cfg))

    def get_balance(self, wallet_id: str) -> int:
        result = self._api.get_wallet_balance(wallet_id)
        return result.balance_minor
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

**Containerising**, in plain terms, means freezing your app and everything it needs to run — Python, dependencies, your code, your config — into a single image that runs identically on your laptop, in CI, and in production. A `Dockerfile` is the recipe for building that image.

`pyfly new` generates a `Dockerfile` for every archetype. For a web service it looks like this after production hardening. Read it top to bottom — each line is one step in the build:

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install uv && \
    uv sync --no-dev --extra web --extra data-relational \
             --extra security --extra observability

COPY src/ src/
COPY pyfly.yaml .

# 8080 = application traffic; 9090 = the management port (actuator + admin).
EXPOSE 8080 9090
CMD ["pyfly", "run", "--host", "0.0.0.0", \
     "--port", "8080", "--server", "granian", "--workers", "2"]
```

Walking the recipe: `FROM` picks a small Python base image; `COPY` + `uv sync` install exactly the dependency extras you name (and nothing else); the second `COPY` adds your source and config; `EXPOSE` documents the two ports the container listens on; `CMD` is the command that runs when the container starts. The `--port 8080` flag here sets `pyfly.server.port` for this process — the management port stays at its `9090` default unless you override `pyfly.management.server.port`.

Install only the extras your service actually uses — the `full` meta-extra pulls in Kafka, RabbitMQ, and MongoDB drivers even when you need none of them.

!!! tip "Run it"
    Build the image and run it, mapping both ports out of the container:

    ```bash
    docker build -t lumen:local .
    docker run --rm -p 8080:8080 -p 9090:9090 lumen:local
    ```

    Then confirm both listeners answer — the business port on `8080` and the
    management port on `9090`:

    ```bash
    curl http://localhost:8080/openapi.json   # app: returns the spec
    curl http://localhost:9090/actuator/health # management: {"status":"UP"}
    ```

    Two ports, one process. That separation is what the rest of this section
    builds on.

### Environment variables and secrets

Never bake secrets into `pyfly.yaml`. PyFly resolves `${ENV_VAR}` placeholders anywhere in configuration:

```yaml
pyfly:
  data:
    relational:
      url: ${DATABASE_URL}

  security:
    jwt:
      secret: ${JWT_SECRET}
```

PyFly reads the actual values from the container environment at startup. In Kubernetes, back those variables with a `Secret`; in Docker Compose, use an `.env` file that is never committed. The `pyfly doctor` command checks that required tools are present but does not validate secrets — that responsibility remains yours.

### Graceful shutdown

PyFly honours graceful shutdown by default. Set `pyfly.server.graceful-timeout` (seconds) to control how long the server waits for in-flight requests to complete before forcing exit:

```yaml
pyfly:
  server:
    graceful-timeout: 30
```

SIGTERM triggers the shutdown sequence: the server stops accepting new connections, `ApplicationContext.stop()` runs `@pre_destroy` hooks and `stop_all()` for plugins, and the process exits cleanly. In Kubernetes set `terminationGracePeriodSeconds` to at least five seconds more than `graceful-timeout`.

### Server selection

The ASGI server is selected by priority at runtime:

| Priority | Server | Install extra |
|---|---|---|
| 1 | Granian (Rust/tokio) | `granian` |
| 2 | Uvicorn | `web` (default) |
| 3 | Hypercorn | `hypercorn` |

For production, prefer Granian — it delivers roughly 3× the throughput of Uvicorn with native HTTP/2. Pair it with uvloop on Linux for an additional 2–4× event-loop speedup:

```bash
uv add "pyfly[web-fast]"   # granian + uvloop in one shot
```

Pin the choice in YAML to avoid surprises on machines where multiple servers happen to be installed:

```yaml
pyfly:
  server:
    type: granian
    event-loop: uvloop
    workers: 4
    graceful-timeout: 30
```

### The two-port deploy

This is the deployment fact you most need to internalise for v26.6.110. PyFly runs on **two ports**, both inside one process:

- the **application port** — `pyfly.server.port`, default `8080` — serves your business API, your WebSocket feeds, the OpenAPI spec, and the config-server routes;
- the **management port** — `pyfly.management.server.port`, default `9090` — serves the Actuator (`/actuator/*`) and the Admin Dashboard (`/admin`).

The management port is a second in-process listener, not extra worker processes, so it costs almost nothing. Two tuning options matter at deploy time: set `pyfly.management.server.port` **equal** to `pyfly.server.port` to collapse everything onto one port, or set it to **`-1`** to disable the management web endpoints entirely. The env override is `PYFLY_MANAGEMENT_SERVER_PORT`.

Why split them? Because it lets you expose only `8080` to the internet while keeping health checks, Prometheus scrapes, and the admin console on `9090`, reachable only from inside your cluster.

!!! warning "The management port is OPEN by default"
    As of v26.6.110 the management port is **unauthenticated by default**
    (Spring Boot's `management.server.port` model): anything that can reach
    `9090` can read `/actuator/*` and `/admin`. That is intentional — the
    port is meant to sit behind network isolation, never on the public
    internet. If you cannot guarantee that isolation, apply the app's
    security filters to the management port too:

    ```yaml
    pyfly:
      management:
        security:
          enabled: true
    ```

    With that flag set, the same authentication, role guards, and CSRF rules
    that protect your business API also gate `9090`.

### Health endpoints

PyFly exposes Spring Boot-style actuator endpoints out of the box. They live on the **management port** (`9090`):

| Endpoint | Purpose |
|---|---|
| `GET /actuator/health` | Aggregate health (UP / DOWN) |
| `GET /actuator/health/liveness` | Kubernetes liveness probe |
| `GET /actuator/health/readiness` | Kubernetes readiness probe |
| `GET /actuator/metrics` | Per-metric drill-down (e.g. `/actuator/metrics/http.server.requests`) |
| `GET /actuator/prometheus` | Prometheus scrape endpoint |

Only `health` and `info` are exposed over HTTP by default (Spring Boot's secure default). To publish metrics and the Prometheus scrape endpoint, add them to the exposure list in `pyfly.yaml`:

```yaml
pyfly:
  management:
    endpoints:
      web:
        exposure:
          include: health,info,metrics,prometheus
    endpoint:
      health:
        show-details: when-authorized
```

!!! tip "Run it"
    Hit the probes on the management port — they answer immediately because
    that port is open by default:

    ```bash
    curl http://localhost:9090/actuator/health
    curl http://localhost:9090/actuator/health/readiness
    ```

    Expected — a JSON status object the orchestrator can parse:

    ```json
    {"status": "UP"}
    ```

    A readiness check that fails (a database still warming up, say) returns
    `{"status": "DOWN"}` with HTTP `503`, which is exactly what Kubernetes
    needs to hold traffic back until the pod is ready.

Then wire your Kubernetes deployment — note every probe and scrape points at the **management port** `9090`, not `8080`:

```yaml
livenessProbe:
  httpGet:
    path: /actuator/health/liveness
    port: 9090
  initialDelaySeconds: 10
  periodSeconds: 15
readinessProbe:
  httpGet:
    path: /actuator/health/readiness
    port: 9090
  initialDelaySeconds: 5
  periodSeconds: 10
```

!!! spring "Spring parity"
    The two-port split is a direct port of Spring Boot's
    `management.server.port`: app traffic on `server.port`, actuator and
    management UI on a dedicated `management.server.port`. The open-by-default
    posture, the `-1`-to-disable convention, and `management.security.enabled`
    to lock it down all mirror Spring Boot's behaviour.

!!! note "Custom probes from the live HealthAggregator"
    New in v26.6.110, the live `HealthAggregator` is reachable at
    `app.state.pyfly_health_aggregator` (Starlette adapter only). Register an
    extra readiness indicator after `create_app()` — for example a check that
    pings a downstream service — and it shows up on `/actuator/health`
    regardless of whether the actuator runs on the shared or the separate
    management port.

**What just happened.** You now have the full shape of a PyFly deployment:
one container, two ports (`8080` for users, `9090` for ops), secrets injected
as environment variables, a Granian server tuned with explicit workers, a
graceful-shutdown window that drains in-flight requests, and Kubernetes
probes wired to the management port. The checklist below is the same picture
as a list you can paste into a pull-request template.

### The production checklist

- [ ] All secrets are environment variables — none are in `pyfly.yaml`
      or source control.
- [ ] Docker image installs only the extras the service uses.
- [ ] Server is pinned to Granian + uvloop; `workers` is set
      explicitly (not left at `1` for multi-core machines).
- [ ] `graceful-timeout` is at least 15 s; Kubernetes
      `terminationGracePeriodSeconds` is at least 5 s more.
- [ ] Liveness and readiness probes point at the **management port**
      (`9090`), not the application port, and are tested.
- [ ] `/actuator/health` (on `9090`) returns UP before traffic is sent.
- [ ] The management port is network-isolated, or
      `pyfly.management.security.enabled: true` is set — it is open by
      default.
- [ ] The Prometheus scrape endpoint (`/actuator/prometheus` on `9090`) is
      added to the exposure list and scraped by the monitoring stack.
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

Seventeen chapters ago you typed `@pyfly_application` and watched the DI container wire your first `@service`. Today that same container powers a production wallet platform. Here is what you constructed along the way.

**Chapters 1–3** gave you the foundation: a first-class DI container, flexible configuration (YAML, env, profiles, SpEL expressions), and a complete HTTP layer with request mapping, filters, content negotiation, and a JSON serialisation layer you can replace without touching a single controller.

**Chapters 4–6** introduced persistence. You mapped entities with SQLAlchemy, managed schema evolution with Alembic, and structured the domain with DDD tactical patterns — aggregates, value objects, and repositories that keep business logic out of infrastructure.

**Chapters 7–9** brought the architecture alive. CQRS split reads from writes at the handler level. EDA let services react to events without polling. Event sourcing made every state change a first-class fact — replayable, auditable, and the foundation for projections.

**Chapters 10–12** taught Lumen to leave its own process. Resilient HTTP clients called downstream services without cascading failures. Sagas orchestrated multi-step transactions across service boundaries with automatic compensation when any step went wrong.

**Chapters 13–15** hardened the platform. Caching cut database pressure. Rate limiters, bulkheads, timeouts, and circuit breakers turned every dependency into a controlled blast radius. Distributed tracing, structured logging, and a live admin dashboard gave you eyes inside the system at every layer.

**Chapters 16–17** closed the feedback loop. A structured test suite — unit, integration, and Testcontainers-backed persistence tests — made the platform safe to change. Scheduled tasks, push notifications, webhooks, and callbacks let Lumen reach out to the world on its own schedule.

**Chapter 18** showed you what lies beyond the core: a plugin system for open extension, a YAML rule engine for business-owned logic, a Config Server for fleet-wide configuration, i18n for global audiences, WebSocket for real-time UX, a Shell module for operational tooling, an OpenAPI spec that generates typed client SDKs automatically, and the production habits that keep all of it running — packaged as one container that listens on two ports, `8080` for users and `9090` for operations.

PyFly is not magic. Every abstraction in this book has a cost, and you now understand what that cost is: a DI container that starts in milliseconds but requires you to think about bean scopes; an async HTTP server that handles thousands of concurrent connections but requires you to avoid blocking calls; a saga engine that survives partial failures but requires you to write compensating transactions.

Understanding the cost is what separates a practitioner from a reader who merely copies patterns. You are now a practitioner.

---

## Try it yourself {.exercises}

1. **Add a custom plugin.** Implement an `AuditPlugin` that contributes an extension to an `"audit-sinks"` extension point. The extension class must implement an `AuditSink` interface and write each event to a file. In a test, call `PluginManager.registry.get("audit-sinks")` and assert that your extension is returned with the expected `name`.

2. **Ship a rules change without redeployment.** Store `transaction_rules.yaml` in the Config Server (`pyfly.config-server.enabled: true`). Write a `RiskService` that fetches the YAML via `ConfigClient` on each call to `assess()` (or caches it with a short TTL). Update the `value` threshold through the `POST /{app}/{profile}` route and verify that `assess()` picks up the new value without restarting the service.

3. **Localise a rejection message.** Add `wallet.limit_exceeded` to `i18n/messages_en.yaml` and `i18n/messages_es.yaml`. Wire a `NotificationService` that reads the locale from the `Accept-Language` header and returns the correct string. Write two tests — one with `Accept-Language: en`, one with `Accept-Language: es` — and assert each returns the right message.

---

Lumen is ready for production. For what comes next — new modules, community plugins, and release notes — visit the framework documentation at [github.com/fireflyframework/fireflyframework-pyfly](https://github.com/fireflyframework/fireflyframework-pyfly). Every concept in this book lives in that repository; the source is the ultimate reference.
