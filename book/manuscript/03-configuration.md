<span class="eyebrow">Chapter 3</span>

# Configuration, Profiles & Secrets {.chtitle}

::: figure art/openers/ch03.svg | &nbsp;

Lumen now has wired services — a `WalletService` backed by a repository, an event publisher, and a full DI lifecycle. The trouble is that every environment where Lumen runs — your laptop, a shared staging cluster, a hardened production deployment — needs different settings. Different ports, different database URLs, different log verbosity, different secrets. Baking those differences into code is fragile; scattering them across a dozen `os.environ` calls is unreadable.

This chapter shows you how PyFly solves that with a single `pyfly.yaml`, a four-layer precedence system, environment-specific overlay files, and strongly-typed configuration classes. By the end, Lumen will have a clean configuration story that scales from `pyfly run` on your laptop all the way to a containerised production deploy — without touching a line of business logic.

---

## pyfly.yaml: your single source of settings

Every non-trivial application has at least two audiences for its configuration: a developer who wants verbose logs and a relaxed database on localhost, and a production system that demands structured JSON logs, a real connection pool, and no debug mode. The naive solution — `if os.getenv("ENV") == "prod":` scattered through a dozen files — quickly becomes impossible to audit. PyFly's answer is one canonical YAML (or TOML) file that holds everything your application knows about itself, with separate mechanisms for varying what changes between environments.

PyFly auto-discovers this file in your project root. The framework checks candidates in order — `pyfly.yaml`, `pyfly.toml`, `config/pyfly.yaml`, `config/pyfly.toml` — and loads the first one it finds. For Lumen, create `pyfly.yaml` in the project root:

::: listing pyfly.yaml | Listing 3.1 — Lumen's base configuration file
pyfly:
  app:
    name: "lumen"
    version: "0.1.0"
    description: "Lumen — fintech wallet and ledger service"

  profiles:
    active: "dev"

  web:
    port: 8080
    host: "0.0.0.0"
    debug: false
    docs:
      enabled: true

  logging:
    level:
      root: "INFO"
    format: "console"

  data:
    enabled: false
    url: "sqlite+aiosqlite:///lumen.db"
    pool-size: 5

lumen:
  wallet:
    daily-transfer-limit: 10000
    default-currency: "USD"
:::

A few things to notice here. The `pyfly:` top-level key is reserved exclusively for framework settings — web server, logging, data access, and profiles all live there. Anything outside `pyfly:` is yours: the `lumen:` section holds domain-specific settings that the framework ignores but that your own code can read through exactly the same API. This means Lumen's business rules (`daily-transfer-limit`, `default-currency`) are right there in the configuration file alongside the infrastructure settings, with no special treatment required.

Nested keys map directly to dot-notation access through `Config.get()`:

```python
config.get("pyfly.web.port")                 # 8080
config.get("pyfly.data.pool-size")           # 5  (kebab and snake interchangeable)
config.get("lumen.wallet.daily-transfer-limit")  # 10000
```

`Config.get()` uses **relaxed segment matching**: `pool-size` and `pool_size` are treated as the same key, so your YAML can use kebab-case (the conventional YAML style) and your Python code can use snake_case — they always resolve to the same value. You never have to remember which form you used in the file.

PyFly uses `PyYAML` (`yaml.safe_load`) for YAML parsing; YAML's native types (integers, booleans, floats) are preserved. The integer `8080` in YAML arrives as an integer in Python — no string parsing needed.

!!! tip "Tip"
    You can also use TOML if your team prefers INI-like syntax with strict typing. Rename the file to `pyfly.toml` and use TOML table syntax — `[pyfly.web]`, `[pyfly.data]` — instead of YAML nesting. Every feature described in this chapter works identically with both formats.

---

## How configuration is layered

A single file works well when you have one environment. Real projects have three or four — development, test, staging, production — and the differences between them are usually small: a database URL here, a log level there. If you duplicate the entire file for each environment, you create a maintenance burden; the first time someone updates the port in one file and forgets the others, you have a configuration drift bug.

PyFly avoids this by layering four configuration sources, each deeply merged on top of the previous. Later layers win:

::: figure art/figures/03-config.svg | Figure 3.1 — Configuration precedence (later layers win).

**Layer 1 — Framework defaults.** The bundled `pyfly-defaults.yaml` inside the `pyfly.resources` package provides a sensible default for every key the framework reads. You never edit this file — it is loaded via `importlib.resources` so it works correctly in packaged distributions. The framework starts from a complete, working baseline.

**Layer 2 — User configuration file.** Your `pyfly.yaml` (or `pyfly.toml`). You only need to include the keys that differ from the framework defaults. In Listing 3.1, `pyfly.web.port: 8080` is actually the same as the default — you include it for clarity, not necessity.

**Layer 3 — Profile overlay files.** For each active profile, PyFly looks for a file named `pyfly-{profile}.yaml` alongside the base file and deep-merges it in. Profile overlays only need the keys that change.

**Layer 4 — Environment variables.** Checked at **read time** on every `Config.get()` call, not baked in at startup. This means an env var set after the application starts still wins — ideal for container deployments where secrets are injected at runtime. Environment variables always override everything else.

### Deep merge, not replacement

Layers are combined using a recursive deep merge (`Config._deep_merge()`). Nested dictionaries are merged key-by-key; scalar values are replaced. This distinction matters more than it might seem: without deep merge, a production overlay that changes only the port would wipe out the `host` and `docs` keys that sit alongside it in the same `web:` section. With deep merge, you only write what you mean to change.

To make this concrete, consider a base file and a prod overlay:

```yaml
# pyfly.yaml (base)
pyfly:
  web:
    port: 8080
    host: "0.0.0.0"
    docs:
      enabled: true
```

```yaml
# pyfly-prod.yaml (overlay)
pyfly:
  web:
    port: 443
```

After merge, the effective configuration is:

```yaml
pyfly:
  web:
    port: 443         # overridden by prod overlay
    host: "0.0.0.0"   # preserved from base
    docs:
      enabled: true   # preserved from base
```

Only the keys that differ need to appear in the overlay. Everything else is preserved from the layer below.

!!! spring "Spring parity"
    This four-layer model maps directly to Spring Boot's configuration hierarchy: `application.yaml` (defaults embedded in the jar) → your `application.yaml` → `application-{profile}.yaml` → environment variables. The deep-merge behaviour, the env-var-always-wins rule, and the early profile resolution step are all deliberate parity decisions.

---

## Profiles

The layering system gives you the mechanism to vary configuration between environments. Profiles give you the vocabulary to name those environments and activate them cleanly, without any `if/else` logic in your application code.

A **profile** is a named environment variant — `dev`, `test`, `staging`, `prod`. Activating a profile causes PyFly to load an overlay file and can conditionally include or exclude beans.

### Activating profiles

PyFly resolves the active profile through **early profile resolution** — it must know which profiles are active *before* loading the full configuration, because it needs to know which overlay files to merge. `PyFlyApplication._resolve_profiles_early()` handles this with a deliberate priority order:

1. **`PYFLY_PROFILES_ACTIVE` environment variable** — highest priority. Comma-separated for multiple profiles.
2. **`pyfly.profiles.active` in the base config file** — fallback when the env var is not set.
3. **Passed programmatically** — via `Config.from_file("pyfly.yaml", active_profiles=["prod"])`.

In Listing 3.1, `profiles.active: "dev"` activates the `dev` profile by default, so every developer who clones the repository gets verbose logging and debug mode without any extra setup. In production you override it with an env var — no code change, no file edit:

```bash
PYFLY_PROFILES_ACTIVE=prod python main.py
```

### Profile overlay files

For each active profile `{name}`, PyFly looks for `pyfly-{name}.yaml` next to the base file. Here are Lumen's three overlays, each containing only the keys that actually differ from the base:

::: listing pyfly-dev.yaml | Listing 3.2 — Development overlay: verbose logging, debug mode
pyfly:
  web:
    debug: true
  data:
    echo: true
  logging:
    level:
      root: "DEBUG"
:::

The dev overlay turns on debug mode so Starlette surfaces detailed tracebacks, enables SQL echo so you can see every query in the terminal, and drops the log level to `DEBUG` so framework internals are visible. Three keys — everything else comes unchanged from the base file.

::: listing pyfly-test.yaml | Listing 3.3 — Test overlay: in-memory SQLite, silent banner
pyfly:
  banner:
    mode: "OFF"
  data:
    enabled: false
  logging:
    level:
      root: "WARNING"
:::

The test overlay silences the startup banner so test output is clean, disables data persistence (unit tests mock the repository layer), and raises the log threshold to `WARNING` so passing tests produce no noise.

::: listing pyfly-prod.yaml | Listing 3.4 — Production overlay: real database, JSON logging, docs off
pyfly:
  web:
    port: 443
    debug: false
    docs:
      enabled: false
  data:
    enabled: true
    url: "postgresql+asyncpg://prod-db:5432/lumen"
    pool-size: 20
  logging:
    level:
      root: "WARNING"
    format: "json"
  banner:
    mode: "OFF"

lumen:
  wallet:
    daily-transfer-limit: 50000
:::

The production overlay makes several deliberate choices worth explaining. It disables the interactive API docs (`enabled: false`) — you do not want a live Swagger UI on a production endpoint. It switches logging to `json` format so log aggregators like Datadog or CloudWatch can parse structured fields rather than scraping human-readable text. It bumps the connection pool to 20 and points `url` at the real PostgreSQL instance. And it raises `daily-transfer-limit` to 50,000 — production wallets have a higher business limit than the 10,000 default, and that domain rule lives here in configuration rather than buried in code.

!!! tip "Tip"
    Multiple profiles are comma-separated in the env var and are applied in order, so the last profile wins on conflicts: `PYFLY_PROFILES_ACTIVE=prod,metrics` first applies `pyfly-prod.yaml`, then `pyfly-metrics.yaml`. Use this to compose cross-cutting concerns — a `metrics` profile can enable Prometheus scraping without duplicating your entire prod config.

### Profile-scoped beans

Sometimes the difference between environments is not just a value — it is whether a whole component exists at all. A seed loader that populates test wallets should never run in production. A verbose audit logger that logs every field of every request is useful in development but a compliance risk in prod.

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

Profile expressions are evaluated by `Environment.accepts_profiles()` during the first pass of `ApplicationContext.start()`. Beans whose profile expression does not match the active set are removed before any resolution takes place — they are never instantiated, never wired, never present in the container. The result is a container that is structurally different per environment without any `if` statement in your application code.

---

## Type-safe settings with @config_properties

String-key lookups like `config.get("lumen.wallet.daily-transfer-limit")` work, but they do not scale well. Each call is an isolated read with no type information — you must remember to call `float()` on the result, and a typo in the key surfaces at the moment a request hits that code path in production, not the moment the application starts. For anything beyond a handful of scattered values, a better approach is to group related settings into a typed Python class that is populated once at startup and injected wherever it is needed.

`@config_properties` solves this by binding a config section to a typed Python dataclass.

### Declaring a properties class

Decorate a `@dataclass` with `@config_properties(prefix="...")`. The `prefix` tells PyFly which config section to bind; field names must match the keys under that section (kebab/snake interchangeable):

::: listing lumen/config.py | Listing 3.5 — WalletProperties: typed settings for the wallet subsystem
from dataclasses import dataclass
from pyfly.core import config_properties


@config_properties(prefix="lumen.wallet")
@dataclass
class WalletProperties:
    daily_transfer_limit: float = 10_000.0
    default_currency: str = "USD"


@config_properties(prefix="pyfly.web")
@dataclass
class LumenWebProperties:
    port: int = 8080
    host: str = "0.0.0.0"
    debug: bool = False
:::

The decorator sets `__pyfly_config_prefix__` on the class. Field types must be `int`, `float`, `bool`, or `str` for coercion to work correctly; more complex types are left as-is.

Notice that each field carries a default value matching the base `pyfly.yaml`. This is intentional: the class is self-documenting, and a `@config_properties` class can be constructed and used in unit tests without any YAML file on disk — just instantiate `WalletProperties()` and you get the development defaults.

### Binding and injecting

Call `config.bind(PropertiesClass)` to produce a populated, typed instance. Because the `Config` object is registered as a singleton bean, you can inject it into any service and bind from there:

::: listing lumen/wallet_service.py | Listing 3.6 — Injecting WalletProperties into WalletService
from pyfly.container import service
from pyfly.core import Config

from lumen.config import WalletProperties
from lumen.wallet_repository import WalletRepository
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
        self.props: WalletProperties = config.bind(WalletProperties)

    async def transfer(
        self, from_id: str, to_id: str, amount: float
    ) -> dict:
        limit = self.props.daily_transfer_limit
        if amount > limit:
            raise ValueError(
                f"Amount {amount} exceeds daily limit {limit} "
                f"{self.props.default_currency}"
            )
        # ... perform transfer ...
        return {"from": from_id, "to": to_id, "amount": amount}
:::

Walk through what happens when the DI container starts Lumen. `WalletService.__init__` receives the shared `Config` singleton and immediately calls `config.bind(WalletProperties)`. That call resolves the bound values once, at startup, and stores them in `self.props` — a plain Python dataclass with real types. From that point on, `transfer()` reads `self.props.daily_transfer_limit` as a `float`, with full IDE autocompletion and no parsing code in sight.

`config.bind()` works through these steps:

1. Reads `__pyfly_config_prefix__` from the class.
2. Calls `effective_section(prefix)` — a resolved copy of the subtree with `${...}` placeholders expanded, environment-variable overrides applied, and env-only keys injected.
3. Matches section keys to dataclass fields using relaxed (kebab/snake interchangeable) lookup.
4. Applies type coercion for fields whose values arrived as strings (for example, from environment variables).
5. Constructs the dataclass with the gathered kwargs; fields absent from the config use their dataclass default values.

The key detail in step 2 is that `effective_section()` applies the full four-layer stack — defaults, file, profile overlay, env vars — before the dataclass is constructed. By the time `bind()` finishes, `WalletProperties` reflects whatever the production overlay or an injected env var says, not just the base YAML.

### Type coercion

YAML parsers produce correctly-typed values for native types — integers, booleans, and floats come through without any coercion. When a value arrives from an environment variable (always a string) and the target field has a non-string type, `bind()` coerces automatically:

| Target type | Coercion rule |
|---|---|
| `int` | `int(value)` |
| `float` | `float(value)` |
| `bool` | `value.lower() in ("true", "1", "yes")` |
| `str` | no coercion needed |

If `bind()` is called on a class that is not decorated with `@config_properties`, it raises `ValueError` immediately — a clear fail-fast signal at startup rather than a silent wrong-value bug at request time.

!!! spring "Spring parity"
    `@config_properties` is PyFly's answer to Spring Boot's `@ConfigurationProperties`. The mental model is identical: annotate a POJO (here, a dataclass) with a prefix, and the framework binds the matching config section to it with full type coercion. The combination of `pyfly.yaml` + profile overlays + `@config_properties` maps to `application.yaml` + `application-{profile}.yaml` + `@ConfigurationProperties` + Spring profiles — same concepts, Pythonic idioms.

---

## Environment variables & secrets

Files are the right home for configuration that varies by environment but is safe to store — ports, log levels, database hostnames. They are the wrong home for secrets: passwords, API keys, signing tokens, and database credentials must never enter source control. The fourth layer of the configuration stack exists specifically to receive these values at deploy time, from a secrets manager or a CI/CD pipeline, without any of them touching the file system.

The fourth and highest-priority layer is environment variables. PyFly checks them on every `Config.get()` call — at read time, not at startup — so they always win, even when set after the process begins.

### Naming convention

Every dot-notation config key maps to a `PYFLY_`-prefixed environment variable through a four-step transformation:

1. Strip the `pyfly.` prefix (if present).
2. Replace dots (`.`) and hyphens (`-`) with underscores (`_`).
3. Uppercase the result.
4. Prefix with `PYFLY_`.

| Config key | Environment variable |
|---|---|
| `pyfly.web.port` | `PYFLY_WEB_PORT` |
| `pyfly.web.debug` | `PYFLY_WEB_DEBUG` |
| `pyfly.data.url` | `PYFLY_DATA_URL` |
| `pyfly.data.pool-size` | `PYFLY_DATA_POOL_SIZE` |
| `pyfly.logging.level.root` | `PYFLY_LOGGING_LEVEL_ROOT` |
| `pyfly.profiles.active` | `PYFLY_PROFILES_ACTIVE` |

For application-specific keys that do not start with `pyfly.`, the full dotted path is transformed the same way (no prefix stripping):

```
lumen.wallet.daily-transfer-limit  →  PYFLY_LUMEN_WALLET_DAILY_TRANSFER_LIMIT
```

The rule is mechanical and consistent, which matters in practice: when you need to tell a Kubernetes operator which env var controls a given setting, the answer is always "apply the four-step transformation" rather than hunting through framework source code.

### Env vars always win

Activating production and overriding the database URL for a specific container instance is a one-liner:

```bash
PYFLY_PROFILES_ACTIVE=prod \
  PYFLY_DATA_URL="postgresql+asyncpg://rds-prod:5432/lumen" \
  PYFLY_WEB_PORT=8080 \
  python main.py
```

Here, `PYFLY_WEB_PORT=8080` overrides the prod overlay's `port: 443`. The precedence stack becomes:

1. Framework defaults → `port: 8080`
2. Base config → `port: 8080` (unchanged)
3. Prod overlay → `port: 443`
4. Env var → `port: 8080` (wins)

Final effective port: `8080`. The prod overlay's value is superseded by the env var, and the base file's value never mattered once the overlay loaded.

This is a useful pattern during a staged migration: you can keep `port: 443` in the overlay as the intended production default, while a temporary env var holds the service on `8080` for a traffic-splitting experiment. When the experiment ends, you remove the env var and the overlay takes over — no file edits needed.

### Keeping secrets out of files

Config files should never contain credentials, API keys, or signing secrets. The `pyfly-defaults.yaml` ships with a placeholder JWT secret (`"change-me-in-production"`) that exists only to keep the framework runnable out of the box. You must replace it in production:

```bash
PYFLY_SECURITY_JWT_SECRET="$(vault kv get -field=jwt_secret secret/lumen)"
```

!!! warning "Never commit secrets"
    Do not put passwords, API keys, database credentials, or JWT secrets in `pyfly.yaml`, `pyfly-prod.yaml`, or any file that enters source control. Use environment variables sourced from a secret manager (HashiCorp Vault, AWS Secrets Manager, Kubernetes Secrets, or similar). The env-var layer exists precisely to receive these at deploy time, not at development time.

### A note on env-only keys

`Config.bind()` also handles values that exist *only* as environment variables — no matching entry in any YAML file. `effective_section()` injects these env-only keys into the bound section so `bind()` sees the same value that `get()` would. This means you can add a new field to a `@config_properties` class, set it exclusively via an env var in your deployment pipeline, and it will be populated correctly even when the YAML files have not been updated:

```bash
# No YAML entry for lumen.wallet.daily-transfer-limit in your CI environment?
# Set it exclusively via env var — bind() still picks it up.
PYFLY_LUMEN_WALLET_DAILY_TRANSFER_LIMIT=25000 python main.py
```

This is a practical escape hatch during incremental rollouts: the team deploying to production can inject a new value before the YAML file has been updated and reviewed, and the application will pick it up without a code change.

---

## What you built {.recap}

Lumen now has a clean configuration story across three environments. A `pyfly.yaml` holds the shared baseline; `pyfly-dev.yaml`, `pyfly-test.yaml`, and `pyfly-prod.yaml` hold only the per-environment deltas. Activating a profile is a single env var (`PYFLY_PROFILES_ACTIVE=prod`). Typed settings live in `WalletProperties` — a dataclass bound at startup with full type coercion — so `WalletService` reads a float limit rather than calling `float(os.environ.get(...))` scattered through service code. Secrets stay in environment variables, never in files.

The four-layer stack — defaults → file → profile overlay → env vars — gives you a single mental model that works from `pyfly run` on your laptop to a locked-down container with secrets injected at deploy time, without touching a line of business logic.

---

## Try it yourself {.exercises}

1. **Add a staging overlay.** Create `pyfly-staging.yaml` with a PostgreSQL URL for a shared test database, `pool-size: 10`, and logging at `INFO`. Activate it with `PYFLY_PROFILES_ACTIVE=staging python main.py` and verify from the startup log that the staging source was loaded. Compare the effective configuration to what the prod overlay would produce.

2. **Bind a new typed property and use it.** Add a `max_wallets_per_owner: int = 5` field to `WalletProperties` and a matching `lumen.wallet.max-wallets-per-owner: 5` key in `pyfly.yaml`. Inject `WalletProperties` into `WalletService` and add a guard in `create_wallet` that raises `ValueError` when the owner already holds the maximum number of wallets. Write a quick test that overrides the limit to `1` by setting `PYFLY_LUMEN_WALLET_MAX_WALLETS_PER_OWNER=1` and verifying the error fires on the second wallet.

3. **Override a value via an env var and observe precedence.** Set `PYFLY_WEB_PORT=9090` before starting Lumen with the `dev` profile active. Check the startup log and confirm the server binds to `9090`, not the `8080` in `pyfly.yaml`. Then unset the env var and restart — the port should revert to `8080`. This exercise makes the read-time nature of env-var resolution concrete: the env var always wins, and removing it immediately restores the file value without any code change.
