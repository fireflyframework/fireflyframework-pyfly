<span class="eyebrow">Chapter 3</span>

# Configuration, Profiles & Secrets {.chtitle}

::: figure art/openers/ch03.svg | &nbsp;

Lumen now has wired services — a `WalletService` backed by a repository, an event publisher, and a full DI lifecycle. The trouble is that every environment where Lumen runs — your laptop, a shared staging cluster, a hardened production deployment — needs different settings: different ports, different database URLs, different log verbosity, different secrets. Baking those differences into code is fragile; scattering them across a dozen `os.environ` calls is unreadable and impossible to audit.

This chapter shows how PyFly solves that with a single `pyfly.yaml`, a four-layer precedence system, environment-specific overlay files, and strongly-typed configuration classes. By the end, Lumen will have a clean configuration story that scales from `pyfly run` on your laptop to a containerised production deploy — without touching a line of business logic.

---

## pyfly.yaml: your single source of settings

Every non-trivial application has at least two audiences for its configuration: a developer who wants verbose logs and a relaxed local database, and a production system that demands structured JSON logs, a real connection pool, and no debug mode. The naive solution — `if os.getenv("ENV") == "prod":` scattered through a dozen files — quickly becomes impossible to audit. PyFly's answer is one canonical YAML (or TOML) file that holds everything your application knows about itself, with separate mechanisms for what changes between environments.

PyFly auto-discovers this file in your project root. The framework checks candidates in order — `pyfly.yaml`, `pyfly.toml`, `config/pyfly.yaml`, `config/pyfly.toml` — and loads the first one it finds. Here is Lumen's base `pyfly.yaml`:

::: listing pyfly.yaml | Listing 3.1 — Lumen's base configuration file
pyfly:
  app:
    name: lumen
    version: 1.0.0
  banner:
    mode: console
  web:
    port: 8080
  observability:
    metrics:
      enabled: true
    tracing:
      enabled: false
  cqrs:
    enabled: true
  transactional:
    enabled: true
    persistence:
      provider: in-memory
  eventsourcing:
    enabled: true
  cache:
    provider: in-memory
  # Event-Driven Architecture: in-memory bus (no broker needed).
  # The EventPublisher bean that wallet command handlers publish
  # through — and that @event_listener projections subscribe to —
  # is activated by setting provider to "memory".
  eda:
    provider: memory
  # Relational data layer (SQLAlchemy + SQLite via aiosqlite).
  # The framework creates the schema on startup (ddl-auto=create).
  data:
    relational:
      enabled: true
      url: "sqlite+aiosqlite:///./lumen.db"
      ddl-auto: create
:::

Three things are worth noting. First, the `pyfly:` top-level key is reserved exclusively for framework settings — web server, observability, CQRS, EDA, data access, and profiles all live there. Your own application keys go under a different top-level name (such as `lumen:`). Second, `pyfly.app.name` and `pyfly.app.version` identify the service throughout — startup banner, health endpoints, and trace metadata all read these values. Third, the `pyfly.data.relational.*` block configures the SQLAlchemy/aiosqlite layer; `url`, `ddl-auto`, and `enabled` are its three core keys.

Nested keys map directly to dot-notation access through `Config.get()`:

```python
config.get("pyfly.app.name")       # "lumen"
config.get("pyfly.server.port")            # 8080
config.get("pyfly.data.relational.url")    # "sqlite+aiosqlite:///./lumen.db"
config.get("pyfly.eda.provider")           # "memory"
```

`Config.get()` uses **relaxed segment matching**: `ddl-auto` and `ddl_auto` resolve to the same key. Your YAML can use kebab-case (the conventional YAML style) and your Python code can use snake_case — no need to remember which form you used in the file.

PyFly uses `PyYAML` (`yaml.safe_load`) for YAML parsing; native YAML types are preserved. The integer `8080` in YAML arrives as a Python `int` — no string parsing required.

!!! tip "Tip"
    You can also use TOML if your team prefers INI-like syntax with strict typing. Rename the file to `pyfly.toml` and use TOML table syntax — `[pyfly.web]`, `[pyfly.data.relational]` — instead of YAML nesting. Every feature described in this chapter works identically with both formats.

---

## How configuration is layered

A single file works well with one environment. Real projects have three or four — development, test, staging, production — and the differences between them are usually small: a database URL here, a log level there. Duplicating the entire file for each environment is a maintenance burden; the first time someone updates the port in one file and forgets the others, you have a configuration drift bug.

PyFly avoids this by layering four configuration sources, each deeply merged on top of the previous. Later layers always win:

::: figure art/figures/03-config.svg | Figure 3.1 — Configuration precedence (later layers win).

**Layer 1 — Framework defaults.** The bundled `pyfly-defaults.yaml` inside the `pyfly.resources` package provides a sensible default for every key the framework reads. You never edit this file — it is loaded via `importlib.resources` and works correctly in packaged distributions. The framework always starts from a complete, working baseline.

**Layer 2 — User configuration file.** Your `pyfly.yaml` (or `pyfly.toml`). Include only the keys that differ from the framework defaults. In Listing 3.1, `pyfly.server.port: 8080` matches the default — it is included for clarity, not necessity.

**Layer 3 — Profile overlay files.** For each active profile, PyFly looks for a file named `pyfly-{profile}.yaml` alongside the base file and deep-merges it in. Profile overlays contain only the keys that change.

**Layer 4 — Environment variables.** Checked at **read time** on every `Config.get()` call, not baked in at startup. This means an env var set after the application starts still wins — the right behaviour for container deployments where secrets are injected at runtime. Environment variables always override everything else.

### Deep merge, not replacement

Layers combine through a recursive deep merge (`Config._deep_merge()`). Nested dictionaries are merged key-by-key; scalar values are replaced. The distinction matters in practice: without deep merge, a production overlay that changes only `pyfly.server.port` would wipe out the `host` key sitting alongside it in the same `server:` section — and the unrelated `web.docs` block in a sibling section. With deep merge, you write only what you mean to change.

To make this concrete, consider a base file and a production overlay:

```yaml
# pyfly.yaml (base)
pyfly:
  server:
    port: 8080
    host: "0.0.0.0"
  web:
    docs:
      enabled: true
```

```yaml
# pyfly-prod.yaml (overlay)
pyfly:
  server:
    port: 443
```

After merge, the effective configuration is:

```yaml
pyfly:
  server:
    port: 443         # overridden by prod overlay
    host: "0.0.0.0"   # preserved from base
  web:
    docs:
      enabled: true   # preserved from base (sibling section untouched)
```

Only the keys that differ appear in the overlay. Everything else is preserved from the layer below.

!!! spring "Spring parity"
    This four-layer model maps directly to Spring Boot's configuration hierarchy: `application.yaml` (defaults embedded in the jar) → your `application.yaml` → `application-{profile}.yaml` → environment variables. The deep-merge behaviour, the env-var-always-wins rule, and the early profile resolution step are all deliberate parity decisions.

---

## Profiles

The layering system provides the mechanism for varying configuration between environments. Profiles provide the vocabulary to name those environments and activate them cleanly, without any `if/else` logic in your application code.

A **profile** is a named environment variant — `dev`, `test`, `staging`, `prod`. Activating a profile loads an overlay file and can conditionally include or exclude beans.

### Activating profiles

PyFly must know which profiles are active *before* loading the full configuration, because it needs to know which overlay files to merge. This **early profile resolution** follows a deliberate priority order:

1. **`PYFLY_PROFILES_ACTIVE` environment variable** — highest priority; comma-separated for multiple profiles.
2. **`pyfly.profiles.active` in the base config file** — fallback when the env var is not set.
3. **Passed programmatically** — via `Config.from_file("pyfly.yaml", active_profiles=["prod"])`.

In production, override with an env var — no code change, no file edit:

```bash
PYFLY_PROFILES_ACTIVE=prod python main.py
```

### Profile overlay files

For each active profile `{name}`, PyFly looks for `pyfly-{name}.yaml` next to the base file. Lumen ships three overlays, each containing only the keys that differ from the base:

::: listing pyfly-dev.yaml | Listing 3.2 — Development overlay: verbose logging, debug mode
pyfly:
  web:
    debug: true
  data:
    relational:
      echo: true
  logging:
    level:
      root: "DEBUG"
:::

Three keys cover everything the dev environment needs: debug mode for detailed tracebacks, SQL echo so every query appears in the terminal, and `DEBUG` log level so framework internals are visible. Everything else comes unchanged from the base file. Note that `echo` lives under `pyfly.data.relational.*`, consistent with the base file structure.

::: listing pyfly-test.yaml | Listing 3.3 — Test overlay: in-memory SQLite, silent banner
pyfly:
  banner:
    mode: "OFF"
  data:
    relational:
      enabled: false
  logging:
    level:
      root: "WARNING"
:::

The test overlay silences the startup banner so test output stays clean, disables data persistence (unit tests mock the repository layer), and raises the log threshold to `WARNING` so passing tests produce no noise.

::: listing pyfly-prod.yaml | Listing 3.4 — Production overlay: real database, JSON logging, docs off
pyfly:
  server:
    port: 443
  web:
    debug: false
    docs:
      enabled: false
  data:
    relational:
      enabled: true
      url: "postgresql+asyncpg://prod-db:5432/lumen"
  logging:
    level:
      root: "WARNING"
    format: "json"
  banner:
    mode: "OFF"
:::

The production overlay makes several deliberate choices. It disables the interactive API docs — you do not want a live Swagger UI on a production endpoint. It switches logging to `json` format so aggregators like Datadog or CloudWatch can parse structured fields rather than scraping human-readable text. It points `pyfly.data.relational.url` at the real PostgreSQL instance. It sets `pyfly.server.port: 443`, though in practice you will override this with `PYFLY_SERVER_PORT` from your deployment pipeline so no topology details enter the repository.

!!! tip "Tip"
    Multiple profiles are comma-separated in the env var and are applied in order, so the last profile wins on conflicts: `PYFLY_PROFILES_ACTIVE=prod,metrics` first applies `pyfly-prod.yaml`, then `pyfly-metrics.yaml`. Use this to compose cross-cutting concerns — a `metrics` profile can enable Prometheus scraping without duplicating your entire prod config.

### Profile-scoped beans

Sometimes the difference between environments is not a value but whether a component exists at all. A seed loader that populates test wallets must never run in production. A verbose audit logger that records every request field is useful in development but a compliance risk in prod.

The `profile` parameter on any stereotype controls when a bean participates in the container. The expression supports negation and comma-separated OR:

```python
from pyfly.container import service


@service(profile="dev")
class DevSeedLoader:
    """Seeds the database with test wallets — only in dev."""
    ...


@service(profile="!prod")
class VerboseAuditLogger:
    """Detailed audit logging — active everywhere except prod."""
    ...
```

`Environment.accepts_profiles()` evaluates profile expressions during the first pass of `ApplicationContext.start()`. Beans whose expression does not match the active set are removed before any resolution takes place — never instantiated, never wired, never present in the container. The result is a container that is structurally different per environment, with no `if` statement in your application code.

---

## Type-safe settings with @config_properties

String-key lookups like `config.get("pyfly.data.relational.url")` work for occasional reads, but they do not scale. Each call is an isolated read with no type information — you must remember to call `float()` on the result, and a typo in a key surfaces at the first request in production, not at startup. For anything beyond a handful of scattered values, the right approach is to group related settings into a typed Python class that is populated once at startup and injected wherever it is needed.

`@config_properties` solves exactly this by binding a config section to a typed Python dataclass.

### Declaring a properties class

Decorate a `@dataclass` with `@config_properties(prefix="...")`. The `prefix` identifies the config section to bind; field names must match the keys under that section (kebab/snake interchangeable). Here is the framework's own `RelationalProperties`, which binds the `pyfly.data.relational.*` block:

::: listing pyfly/config/properties/data.py | Listing 3.5 — RelationalProperties: typed settings for the data layer
from dataclasses import dataclass

from pyfly.core.config import config_properties


@config_properties(prefix="pyfly.data.relational")
@dataclass
class RelationalProperties:
    """Typed binding for pyfly.data.relational.*"""

    enabled: bool = False
    url: str = "sqlite+aiosqlite:///pyfly.db"
    echo: bool = False
    pool_size: int = 5
:::

The decorator sets `__pyfly_config_prefix__` on the class and marks it as an injectable bean. Field types must be `int`, `float`, `bool`, or `str` for automatic coercion; more complex types are left as-is.

Notice that each field carries a default value matching the framework's built-in `pyfly-defaults.yaml`. This is intentional: the class is self-documenting, and it can be constructed and used in unit tests without any YAML file on disk — just instantiate `RelationalProperties()` and you get the development defaults.

Apply the same pattern to your own application-level settings. Here is how a `WalletProperties` class would look for Lumen's business rules:

```python
from dataclasses import dataclass
from pyfly.core import config_properties


@config_properties(prefix="lumen.wallet")
@dataclass
class WalletProperties:
    daily_transfer_limit: float = 10_000.0
    default_currency: str = "USD"
```

Add the matching block to `pyfly.yaml` under the `lumen:` top-level key (outside `pyfly:`) and the framework binds it automatically — no special registration required.

### Binding and injecting

Call `config.bind(PropertiesClass)` to produce a populated, typed instance. `Config` is registered as a singleton bean, so you can inject it into any service and bind from there:

::: listing lumen/wallet_service.py | Listing 3.6 — Injecting RelationalProperties via config.bind()
from pyfly.container import service
from pyfly.core import Config
from pyfly.config.properties import RelationalProperties
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.eda import EventPublisher


@service
class WalletService:
    def __init__(
        self,
        repo: WalletRepository,
        events: EventPublisher,
        config: Config,
    ) -> None:
        self.repo = repo
        self.events = events
        self.db: RelationalProperties = config.bind(
            RelationalProperties
        )

    async def transfer(
        self, from_id: str, to_id: str, amount: float
    ) -> dict:
        if not self.db.enabled:
            raise RuntimeError("Relational layer not enabled")
        # ... perform transfer using self.db.url for diagnostics ...
        return {"from": from_id, "to": to_id, "amount": amount}
:::

When the DI container starts Lumen, `WalletService.__init__` receives the shared `Config` singleton and immediately calls `config.bind(RelationalProperties)`. That call resolves the bound values once, at startup, and stores them in `self.db` — a plain Python dataclass with real types. From that point on, `transfer()` reads `self.db.enabled` as a `bool`, with full IDE autocompletion and no parsing code anywhere.

`config.bind()` works through five steps:

1. Reads `__pyfly_config_prefix__` from the class.
2. Calls `effective_section(prefix)` — a resolved copy of the subtree with `${...}` placeholders expanded, environment-variable overrides applied, and env-only keys injected.
3. Matches section keys to dataclass fields using relaxed (kebab/snake interchangeable) lookup.
4. Applies type coercion for fields whose values arrived as strings (for example, from environment variables).
5. Constructs the dataclass with the gathered kwargs; fields absent from config use their dataclass defaults.

The critical detail in step 2 is that `effective_section()` applies the full four-layer stack — defaults, file, profile overlay, env vars — before the dataclass is constructed. By the time `bind()` finishes, `RelationalProperties` reflects whatever the production overlay or a runtime env var says, not just the base YAML.

### Injecting individual values with Value

For isolated settings that do not warrant a full properties class, PyFly provides a `Value` descriptor. Declare it as a class-level field and the DI container resolves it at bean creation time — exactly like Spring Boot's `@Value("${...}")`:

```python
from pyfly.container import service
from pyfly.core import Value


@service
class WalletService:
    # Resolved from pyfly.app.name in the merged config.
    app_name: str = Value("${pyfly.app.name}")
    # Falls back to 10000 when the key is absent.
    transfer_limit: float = Value(
        "${lumen.wallet.daily-transfer-limit:10000}"
    )
```

`Value("${key}")` raises `KeyError` at startup when the key is missing and no default is provided — a fail-fast guarantee that keeps missing-config bugs out of production. `Value("${key:default}")` uses the colon-delimited default when the key is absent.

### Type coercion

Native YAML types arrive correctly typed — integers, booleans, and floats need no coercion. When a value arrives from an environment variable (always a string) and the target field has a non-string type, `bind()` coerces automatically:

| Target type | Coercion rule |
|---|---|
| `int` | `int(value)` |
| `float` | `float(value)` |
| `bool` | `value.lower() in ("true", "1", "yes", "on")` |
| `str` | no coercion needed |

Calling `bind()` on a class not decorated with `@config_properties` raises `ValueError` immediately — a clear fail-fast signal at startup rather than a silent wrong-value bug at request time.

!!! spring "Spring parity"
    `@config_properties` is PyFly's answer to Spring Boot's `@ConfigurationProperties`. The mental model is identical: annotate a POJO (here, a dataclass) with a prefix, and the framework binds the matching config section to it with full type coercion. `Value("${...}")` maps to Spring's `@Value("${...}")` — same expression syntax, same fail-fast-on-missing guarantee. The combination of `pyfly.yaml` + profile overlays + `@config_properties` + `Value` maps to `application.yaml` + `application-{profile}.yaml` + `@ConfigurationProperties` + `@Value` — same concepts, Pythonic idioms.

---

## Environment variables & secrets

Files are the right home for configuration that varies by environment but is safe to commit — ports, log levels, database hostnames. They are the wrong home for secrets: passwords, API keys, signing tokens, and database credentials must never enter source control. The fourth layer of the configuration stack exists specifically to receive these values at deploy time, from a secrets manager or a CI/CD pipeline, without any of them touching the file system.

Environment variables are the fourth and highest-priority layer. PyFly checks them on every `Config.get()` call — at read time, not at startup — so they always win, even when set after the process begins.

### Naming convention

Every dot-notation config key maps to a `PYFLY_`-prefixed environment variable through a mechanical three-step transformation:

1. Strip the `pyfly.` prefix (if present).
2. Replace dots (`.`) and hyphens (`-`) with underscores (`_`).
3. Uppercase the result and prefix with `PYFLY_`.

| Config key | Environment variable |
|---|---|
| `pyfly.app.name` | `PYFLY_APP_NAME` |
| `pyfly.server.port` | `PYFLY_SERVER_PORT` |
| `pyfly.management.server.port` | `PYFLY_MANAGEMENT_SERVER_PORT` |
| `pyfly.web.debug` | `PYFLY_WEB_DEBUG` |
| `pyfly.data.relational.url` | `PYFLY_DATA_RELATIONAL_URL` |
| `pyfly.data.relational.pool-size` | `PYFLY_DATA_RELATIONAL_POOL_SIZE` |
| `pyfly.logging.level.root` | `PYFLY_LOGGING_LEVEL_ROOT` |
| `pyfly.eda.provider` | `PYFLY_EDA_PROVIDER` |
| `pyfly.profiles.active` | `PYFLY_PROFILES_ACTIVE` |

For application-specific keys that do not start with `pyfly.`, the full dotted path is transformed the same way (no prefix stripping):

```
lumen.wallet.daily-transfer-limit
  →  PYFLY_LUMEN_WALLET_DAILY_TRANSFER_LIMIT
```

The rule is consistent: when you need to tell a Kubernetes operator which env var controls a given setting, the answer is always "apply the three-step transformation" rather than hunting through framework source code.

### Env vars always win

Activating the production profile and overriding the database URL for a specific container instance is a single command:

```bash
PYFLY_PROFILES_ACTIVE=prod \
  PYFLY_DATA_RELATIONAL_URL="postgresql+asyncpg://rds-prod:5432/lumen" \
  PYFLY_SERVER_PORT=8080 \
  python main.py
```

Here, `PYFLY_SERVER_PORT=8080` overrides the prod overlay's `port: 443`. The precedence stack resolves like this:

1. Framework defaults → `port: 8080`
2. Base config → `port: 8080` (unchanged)
3. Prod overlay → `port: 443`
4. Env var → `port: 8080` (wins)

The effective port is `8080`. This is a useful pattern during a staged migration: keep `port: 443` in the overlay as the intended production default, then use a temporary env var to hold the service on `8080` for a traffic-splitting experiment. When the experiment ends, remove the env var and the overlay takes over — no file edits needed.

### Keeping secrets out of files

Config files must never contain credentials, API keys, or signing secrets. The `pyfly-defaults.yaml` ships with a placeholder JWT secret (`"change-me-in-production"`) that exists only to keep the framework runnable out of the box. Replace it before going to production:

```bash
PYFLY_SECURITY_JWT_SECRET="$(vault kv get -field=jwt_secret secret/lumen)"
```

!!! warning "Never commit secrets"
    Do not put passwords, API keys, database credentials, or JWT secrets in `pyfly.yaml`, `pyfly-prod.yaml`, or any file that enters source control. Use environment variables sourced from a secret manager (HashiCorp Vault, AWS Secrets Manager, Kubernetes Secrets, or similar). The env-var layer exists precisely to receive these at deploy time, not at development time.

### A note on env-only keys

`Config.bind()` also handles values that exist *only* as environment variables — no matching entry in any YAML file. `effective_section()` injects these env-only keys into the bound section so `bind()` sees the same value that `get()` would. Add a new field to a `@config_properties` class, set it exclusively via an env var in your deployment pipeline, and it is populated correctly even when the YAML files have not been updated yet:

```bash
# No YAML entry for pyfly.data.relational.pool-size?
# Set it exclusively via env var — bind() still picks it up.
PYFLY_DATA_RELATIONAL_POOL_SIZE=20 python main.py
```

This is a practical escape hatch during incremental rollouts: the deploying team can inject a new value before the YAML file is updated and reviewed, and the application picks it up without a code change.

---

## What you built {.recap}

Lumen now has a clean configuration story across three environments. A `pyfly.yaml` holds the shared baseline — `pyfly.app.name`, `pyfly.eda.provider`, `pyfly.data.relational.*`, and every other framework knob Lumen uses. `pyfly-dev.yaml`, `pyfly-test.yaml`, and `pyfly-prod.yaml` hold only the per-environment deltas. Activating a profile takes a single env var (`PYFLY_PROFILES_ACTIVE=prod`). Typed settings live in `@config_properties` dataclasses, bound at startup with full type coercion — so services read typed fields rather than calling `float(os.environ.get(...))` in scattered service code. Individual values inject cleanly via `Value("${key}")`, which fails fast at startup when the key is missing. Secrets stay in environment variables, never in files.

The four-layer stack — defaults → file → profile overlay → env vars — gives you one mental model that works from `pyfly run` on your laptop to a locked-down container with secrets injected at deploy time, without touching a line of business logic.

---

## Application and management ports

PyFly separates the **application** port from the **management** port, mirroring
Spring Boot's `server.port` / `management.server.port`. Out of the box the
business API listens on `pyfly.server.port` (**8080**) while the actuator
endpoints (`/actuator/*`) and the admin dashboard (`/admin`) are served on a
dedicated `pyfly.management.server.port` (**9090**). This keeps health checks,
Prometheus scraping, and the admin console off the public port — you expose only
8080 to the internet and reach 9090 from inside the cluster.

| Key | Env var | Default | Purpose |
|---|---|---|---|
| `pyfly.server.port` | `PYFLY_SERVER_PORT` | `8080` | Application HTTP port |
| `pyfly.server.host` | `PYFLY_SERVER_HOST` | `0.0.0.0` | Application bind address |
| `pyfly.management.server.port` | `PYFLY_MANAGEMENT_SERVER_PORT` | `9090` | Management (actuator + admin) port |
| `pyfly.management.server.address` | `PYFLY_MANAGEMENT_SERVER_ADDRESS` | app host | Management bind address |

The management port is a second **in-process** listener — not extra worker
processes — sharing the same event loop and beans, so it works with any server
adapter (Granian, Uvicorn, Hypercorn). Two values change the topology: set
`pyfly.management.server.port` **equal** to the app port to serve everything on a
single port (the pre-`v26.06.102` behaviour), or set it to **`-1`** to disable the
management web endpoints entirely.

!!! spring "Spring parity"
    `pyfly.server.port` ≡ Spring `server.port`, `pyfly.server.host` ≡
    `server.address`, and `pyfly.management.server.port` ≡
    `management.server.port`. Setting a distinct management port runs the actuator
    on its own connector, exactly as Spring Boot does.

## Try it yourself {.exercises}

1. **Add a staging overlay.** Create `pyfly-staging.yaml` with a PostgreSQL URL for a shared test database under `pyfly.data.relational.url`, `pyfly.data.relational.enabled: true`, and logging at `INFO`. Activate it with `PYFLY_PROFILES_ACTIVE=staging python main.py` and verify from the startup log that the staging source was loaded. Compare the effective configuration to what the prod overlay would produce.

2. **Bind a new typed property and use it.** Add a `max_wallets_per_owner: int = 5` field to a new `WalletProperties` class decorated with `@config_properties(prefix="lumen.wallet")`, and a matching `lumen.wallet.max-wallets-per-owner: 5` key in `pyfly.yaml` (outside the `pyfly:` block). Inject `Config` into `WalletService`, call `config.bind(WalletProperties)`, and add a guard in `open_wallet` that raises `ValueError` when the owner already holds the maximum number of wallets. Write a quick test that overrides the limit to `1` by setting `PYFLY_LUMEN_WALLET_MAX_WALLETS_PER_OWNER=1` and verifying the error fires on the second wallet.

3. **Override a value via an env var and observe precedence.** Set `PYFLY_SERVER_PORT=9090` before starting Lumen. Check the startup log and confirm the server binds to `9090`, not the `8080` in `pyfly.yaml`. Then unset the env var and restart — the port should revert to `8080`. This exercise makes the read-time nature of env-var resolution concrete: the env var always wins, and removing it immediately restores the file value without any code change.
