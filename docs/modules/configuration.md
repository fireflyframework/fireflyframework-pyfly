# Configuration Guide

This guide covers everything about configuring a PyFly application: file formats, the
layered loading strategy, profiles, environment variable overrides, typed config binding,
and the full reference of framework defaults.

---

## Table of Contents

1. [Introduction](#introduction)
2. [Config Class API](#config-class-api)
   - [Constructor](#constructor)
   - [from_file()](#from_file)
   - [get()](#get)
   - [get_section()](#get_section)
   - [bind()](#bind)
   - [reload_from_sources()](#reload_from_sources)
3. [Runtime Configuration Refresh](#runtime-configuration-refresh)
4. [YAML Configuration](#yaml-configuration)
5. [TOML Configuration](#toml-configuration)
6. [Profile System](#profile-system)
   - [Activating Profiles](#activating-profiles)
   - [Profile-Specific Files](#profile-specific-files)
   - [Profile Expressions in Beans](#profile-expressions-in-beans)
7. [Configuration Layering](#configuration-layering)
   - [Layer 1: Framework Defaults](#layer-1-framework-defaults)
   - [Layer 2: User Configuration File](#layer-2-user-configuration-file)
   - [Layer 3: Profile Overlays](#layer-3-profile-overlays)
   - [Layer 4: Environment Variables](#layer-4-environment-variables)
   - [Deep Merge Behavior](#deep-merge-behavior)
   - [Remote Config Import (Config Server)](#remote-config-import-config-server)
8. [Environment Variable Overrides](#environment-variable-overrides)
   - [Naming Convention](#naming-convention)
   - [Type Coercion](#type-coercion)
   - [Examples](#environment-variable-examples)
9. [@config_properties](#config_properties)
   - [Defining a Config Class](#defining-a-config-class)
   - [Binding at Runtime](#binding-at-runtime)
   - [Type Coercion in bind()](#type-coercion-in-bind)
10. [@Value (Field-Level Config Injection)](#value-field-level-config-injection)
    - [Expression Syntax](#expression-syntax)
    - [Usage in Beans](#usage-in-beans)
    - [@Value vs @config_properties](#value-vs-config_properties)
11. [SpEL-lite Expressions](#spel-lite-expressions)
    - [The `#{ ... }` Form](#the-spel-form)
    - [`@conditional_on_expression`](#conditional_on_expression)
    - [Safety Model](#safety-model)
12. [Framework Defaults Reference](#framework-defaults-reference)
    - [Application](#application-defaults)
    - [Profiles](#profiles-defaults)
    - [Banner](#banner-defaults)
    - [Logging](#logging-defaults)
    - [Web](#web-defaults)
    - [Data](#data-defaults)
    - [Cache](#cache-defaults)
    - [Messaging](#messaging-defaults)
    - [Client](#client-defaults)
    - [Server](#server-defaults)
    - [Admin](#admin-defaults)
    - [Security](#security-defaults)
    - [Observability](#observability-defaults)
13. [Complete Example: Multi-Environment Setup](#complete-example-multi-environment-setup)

---

## Introduction

PyFly's configuration philosophy is **convention over configuration** with **full
override capability**. The framework ships with sensible defaults for every setting. You
only need to configure what differs from the defaults.

Key principles:

- **Layered**: four layers of configuration are deeply merged so you can override at
  any granularity.
- **File-format agnostic**: YAML and TOML are both first-class citizens.
- **Profile-aware**: different environments (dev, staging, prod) are handled with profile
  overlay files, not conditional logic in code.
- **Environment-variable friendly**: every config key can be overridden by an env var,
  making deployments in containers and CI/CD pipelines straightforward.
- **Type-safe binding**: use `@config_properties` to bind config sections to typed
  Python dataclasses.

---

## Config Class API

The `Config` class is the central configuration holder in PyFly. It wraps a nested
dictionary and provides dot-notation access with environment variable overrides.

```python
from pyfly.core import Config
```

### Constructor

```python
class Config:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
```

Creates a `Config` from a pre-built dictionary. Most users will use `from_file()` instead.

```python
config = Config({"pyfly": {"app": {"name": "my-app"}}})
assert config.get("pyfly.app.name") == "my-app"
```

### from_file()

```python
@classmethod
def from_file(
    cls,
    path: str | Path,
    active_profiles: list[str] | None = None,
    load_defaults: bool = True,
) -> Config:
```

Loads configuration from a YAML or TOML file, merging framework defaults and profile
overlays. This is the recommended way to create a `Config` instance.

**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | `str \| Path` | *(required)* | Path to the base configuration file (`.yaml` or `.toml`). |
| `active_profiles` | `list[str] \| None` | `None` | Profiles whose overlay files should be merged. |
| `load_defaults` | `bool` | `True` | Whether to load the bundled `pyfly-defaults.yaml` as the base layer. |

**Merge order (later wins):**

1. Framework defaults (`pyfly-defaults.yaml`)
2. Base config file (the file at `path`)
3. Profile overlay files (`{stem}-{profile}{suffix}` for each active profile, in order)
4. Environment variables (checked at read time in `get()`)

```python
config = Config.from_file("pyfly.yaml", active_profiles=["dev"])
```

### get()

```python
def get(self, key: str, default: Any = None) -> Any:
```

Retrieves a value by dot-notation key. **Environment variables are checked first**, then
the nested dictionary is walked.

**Dot-notation to env var mapping:**

```
Config Key                   -->  Environment Variable
pyfly.app.name               -->  PYFLY_APP_NAME
pyfly.server.port            -->  PYFLY_SERVER_PORT
pyfly.data.pool-size          -->  PYFLY_DATA_POOL_SIZE
database.host                -->  PYFLY_DATABASE_HOST
```

The transformation:
1. If the key starts with `pyfly.`, strip that prefix.
2. Replace dots (`.`) and hyphens (`-`) with underscores (`_`).
3. Uppercase the result.
4. Prefix with `PYFLY_`.

> **One uniform prefix.** Every PyFly setting binds from a single `PYFLY_*`
> environment-variable prefix — there is no `SPRING_*`, `FIREFLY_*` or `IDP_*`
> split. Even identity-provider config follows it (`pyfly.idp.*` ->
> `PYFLY_IDP_*`). The mapping is centralized in `Config._env_key`, so the rule
> above holds for every key in the framework.

If no env var is set, the method walks the nested dictionary using the dot-separated parts.
Returns `default` if the key is not found.

```python
port = config.get("pyfly.server.port", 8080)  # int 8080 from YAML, or str from env var
```

**Relaxed segment matching:** the dictionary walk in `get()` / `_raw_get()` is *relaxed*
(Spring Boot style): each path segment is matched with kebab/snake-case treated as
interchangeable. So `config.get("pyfly.data.pool-size")` finds a value stored under
`pool_size`, and `config.get("my_prop.sub_key")` finds one stored under `my-prop.sub-key`.
An exact match is tried first (hot path); only when absent does it fall back to comparing
the `_relaxed()` form (`-`/whitespace -> `_`, lower-cased) of each key. This is implemented
by `_dict_get_relaxed()` in `core/config.py`.

### get_section()

```python
def get_section(self, prefix: str) -> dict[str, Any]:
```

Returns all values under a dot-notation prefix as a dictionary subtree.

```python
server_config = config.get_section("pyfly.server")
# {"host": "0.0.0.0", "port": 8080, "workers": 0, "type": "auto", ...}
```

If the prefix does not exist, returns an empty dict `{}`.

### bind()

```python
def bind(self, config_cls: type[T]) -> T:
```

Binds a config section to a `@config_properties` dataclass, producing a typed object.
See the [@config_properties](#config_properties) section for details.

```python
@config_properties(prefix="pyfly.server")
@dataclass
class ServerConfig:
    port: int = 8080
    host: str = "0.0.0.0"

server = config.bind(ServerConfig)
print(server.port)  # 8080
```

Raises `ValueError` if the class is not decorated with `@config_properties`.

### reload_from_sources()

```python
def reload_from_sources(self) -> bool:
```

Re-reads the original configuration sources and **atomically swaps in** the freshly
merged result, so a running application picks up edits to the config files and profile
overlays without a restart (Spring Cloud config refresh). It replays the exact merge
recorded by [`from_sources()`](#from_sources) — framework defaults, starter defaults,
`config/` files, project-root files, and profile overlays — under an internal lock, then
rebinds `_data` in a single assignment. Because `get()` reads that single attribute,
concurrent readers always see a consistent snapshot (the old tree or the new one, never a
half-merged mix).

Returns:

| Return | Meaning |
|---|---|
| `True` | The sources were re-read and the merged config was swapped in. |
| `False` | The instance was **not** built via `from_sources()` (e.g. a dict-constructed `Config`), so there is nothing to reload — a no-op. |

```python
config = Config.from_sources(".", active_profiles=["prod"])
# ... edit pyfly.yaml / pyfly-prod.yaml on disk ...
config.reload_from_sources()        # True — files re-read, get() now returns new values
Config({"a": 1}).reload_from_sources()  # False — dict-constructed, nothing to reload
```

> **Note:** environment-variable overrides and `${...}` placeholders are always resolved at
> *read time* in `get()`, so they reflect the current process environment regardless of
> reloading. `reload_from_sources()` is specifically about re-reading the **files**.

This method is invoked automatically by `POST /actuator/refresh` — see
[Runtime Configuration Refresh](#runtime-configuration-refresh) below.

---

## Runtime Configuration Refresh

PyFly supports Spring Cloud-style runtime configuration refresh: you can change config
files (or profile overlays) on disk and have a running application pick up the changes —
no restart — by issuing a single management request.

```bash
curl -X POST http://localhost:8080/actuator/refresh
# {"refreshed": ["FeatureFlags-singleton", "PricingProperties-singleton"]}
```

### What happens on POST /actuator/refresh

The endpoint resolves the injectable `ContextRefresher` and calls its `refresh()`, which
performs the following steps in order (`src/pyfly/context/refresh.py`):

1. **Re-reads the config sources.** It calls `Config.reload_from_sources()`, which replays
   the original multi-source merge and atomically swaps in the new tree. This is the step
   that lets edits to `pyfly.yaml` / `pyfly-{profile}.yaml` take effect at runtime. (For a
   dict-constructed `Config` this is a no-op.)
2. **Evicts all refresh-scoped beans** (`@refresh_scope`). Each cached instance is dropped
   so the next resolution rebuilds it — re-running constructor/field injection and
   re-reading `@Value` placeholders against the now-refreshed `Config`.
3. **Resets `@config_properties` singletons.** Their backing instances are cleared so they
   re-`bind()` from the live `Config` (which now reflects the re-read files, env-var
   overrides, and resolved `${...}` placeholders) on next resolution.
4. **Publishes a `RefreshScopeRefreshedEvent`** on the application event bus.

The response is `{"refreshed": [...]}`, listing the cache keys of the evicted
refresh-scoped beans (an empty list when none are registered).

### Picking up file changes in your beans

Because step 1 now re-reads the files before steps 2–3 rebuild the affected beans, both
`@refresh_scope` and `@config_properties` beans see the **new file values** after a
refresh:

```python
from dataclasses import dataclass
from pyfly.container import component
from pyfly.core import config_properties
from pyfly.container.refresh_scope import refresh_scope
from pyfly.core.value import Value


@config_properties(prefix="pyfly.pricing")
@dataclass
class PricingProperties:
    base_rate: float = 1.0      # edit pyfly.yaml + POST /actuator/refresh -> re-bound


@component
@refresh_scope
class FeatureFlags:
    new_checkout: bool = Value("${features.new-checkout:false}")
    # next resolution after refresh re-reads ${features.new-checkout} from the live Config
```

### Exposure (opt-in)

Like Spring Boot, the actuator is secure-by-default: only `health` and `info` are
reachable over HTTP. The `refresh` endpoint is registered whenever the actuator is enabled
with a context, but it is **not mounted** until you add it to the exposure include list:

```yaml
pyfly:
  management:
    endpoints:
      web:
        exposure:
          include: "health,info,refresh"   # or "*" to expose every enabled endpoint
```

With the default `health,info`, `POST /actuator/refresh` returns `404`. See the
[Actuator guide](actuator.md#refresh-endpoint) for the full endpoint reference.

---

## YAML Configuration

YAML is the default configuration format. PyFly uses `PyYAML` (`yaml.safe_load`) for
parsing.

### File Structure

```yaml
# pyfly.yaml
pyfly:
  app:
    name: "inventory-service"
    version: "2.0.0"

  profiles:
    active: "dev"

  web:
    port: 8080
    host: "0.0.0.0"
    debug: true

  data:
    enabled: true
    url: "postgresql+asyncpg://localhost:5432/inventory"
    pool-size: 10

  logging:
    level:
      root: "DEBUG"
    format: "console"
```

Nested keys map directly to dot-notation access:

```python
config.get("pyfly.data.url")        # "postgresql+asyncpg://localhost:5432/inventory"
config.get("pyfly.data.pool-size")  # 10
```

---

## TOML Configuration

TOML is an alternative configuration format, parsed with Python's built-in `tomllib`
(Python 3.11+). Use `.toml` for projects that prefer INI-like syntax with strict typing.

### File Structure

```toml
# pyfly.toml
[pyfly.app]
name = "inventory-service"
version = "2.0.0"

[pyfly.profiles]
active = "dev"

[pyfly.web]
port = 8080
host = "0.0.0.0"
debug = true

[pyfly.data]
enabled = true
url = "postgresql+asyncpg://localhost:5432/inventory"
pool-size = 10

[pyfly.logging.level]
root = "DEBUG"

[pyfly.logging]
format = "console"
```

Both YAML and TOML produce identical nested dictionary structures. The format is
determined by the file extension (`.yaml` vs `.toml`). All features -- layering, profiles,
env var overrides, `@config_properties` binding -- work identically with both formats.

---

## Profile System

Profiles let you maintain separate configuration for different environments (development,
staging, production, testing) without conditional logic in your code.

### Activating Profiles

Profiles are activated in priority order:

1. **Environment variable** (highest priority):
   ```bash
   PYFLY_PROFILES_ACTIVE=prod,metrics python main.py
   ```

2. **Config file** (fallback):
   ```yaml
   pyfly:
     profiles:
       active: "dev"
   ```

3. **Programmatically** (in `Config.from_file()`):
   ```python
   config = Config.from_file("pyfly.yaml", active_profiles=["prod", "metrics"])
   ```

Multiple profiles are comma-separated. They are applied in order, so the last profile's
values win on conflicts.

### Profile-Specific Files

For each active profile, PyFly looks for a file named `{stem}-{profile}{suffix}` in the
same directory as the base config file.

| Base File | Profile | Overlay File |
|---|---|---|
| `pyfly.yaml` | `dev` | `pyfly-dev.yaml` |
| `pyfly.yaml` | `prod` | `pyfly-prod.yaml` |
| `pyfly.toml` | `staging` | `pyfly-staging.toml` |
| `config/pyfly.yaml` | `test` | `config/pyfly-test.yaml` |

Profile overlay files only need to contain the keys that differ from the base:

```yaml
# pyfly-prod.yaml
pyfly:
  web:
    port: 443
    debug: false
  logging:
    level:
      root: "WARNING"
  data:
    url: "postgresql+asyncpg://prod-host:5432/inventory"
    pool-size: 20
```

### Profile Expressions in Beans

Stereotype decorators (`@service`, `@component`, `@repository`, …) and `@bean` accept a
`profile` parameter that controls when a bean is active — the equivalent of Spring's
`@Profile`. The expression is stored as `__pyfly_profile__` and evaluated by
`Environment.accepts_profiles()` during `ApplicationContext` startup
(`_filter_by_profile()`); beans whose expression does not match the active profiles are
dropped before instantiation.

| Expression | Meaning |
|---|---|
| `"dev"` | Active when the `dev` profile is active. |
| `"!production"` | Active when `production` is **not** active. |
| `"dev,test"` | Active when `dev` **or** `test` is active (legacy comma-OR). |

```python
@service(profile="dev")
class DevOnlyService:
    """Only loaded when 'dev' profile is active."""
    ...

@service(profile="!test")
class ProductionService:
    """Loaded in all profiles except 'test'."""
    ...
```

#### Boolean Profile Expressions

Since **v26.06.39**, profile expressions support the full Spring Boot 2.4+ grammar:
the `&` (and), `|` (or), and `!` (not) operators combined with `()` grouping. This is
evaluated by `Environment.accepts_profiles()` (in `pyfly.context.environment`). The legacy
comma-OR form still works for backward compatibility.

| Expression | Active when… |
|---|---|
| `"prod & cloud"` | both `prod` **and** `cloud` are active. |
| `"prod \| qa"` | either `prod` **or** `qa` is active. |
| `"(prod & cloud) \| qa"` | `prod` and `cloud` are both active, **or** `qa` is active. |
| `"!(dev \| test)"` | neither `dev` **nor** `test` is active. |

```python
@service(profile="prod & cloud")
class CloudMetricsExporter:
    """Only loaded when BOTH 'prod' and 'cloud' profiles are active."""
    ...

@service(profile="!(dev | test)")
class RealPaymentGateway:
    """Loaded in any profile that is not 'dev' and not 'test'."""
    ...
```

You can also evaluate expressions directly against the `Environment`:

```python
from pyfly.core import Config
from pyfly.context.environment import Environment

env = Environment(Config({"pyfly": {"profiles": {"active": "prod,cloud"}}}))

env.accepts_profiles("prod & cloud")        # True
env.accepts_profiles("(prod & cloud) | qa") # True
env.accepts_profiles("!(dev | test)")       # True
env.accepts_profiles("dev,test")            # False (legacy comma-OR still supported)
```

The evaluator is safe by construction: each profile token is substituted with `True`/`False`
and the resulting boolean expression is parsed with `ast.parse(..., mode="eval")` and walked
node-by-node (only `and`/`or`/`not` and grouping are honored), never via Python `eval`. A
malformed expression evaluates to `False` rather than raising.

### Early Profile Resolution

Profiles must be resolved **before** `Config.from_file()` runs, because the method
needs to know which overlay files to merge. This is handled by
`PyFlyApplication._resolve_profiles_early()`, which:

1. Checks the `PYFLY_PROFILES_ACTIVE` environment variable.
2. If not set, reads the base config file (YAML only) and extracts
   `pyfly.profiles.active`.
3. Returns the list of active profiles for use in `Config.from_file()`.

This means profile activation via the config file works even before the full
configuration is loaded.

---

## Application and Management Server Ports

PyFly serves the actuator endpoints (`/actuator/*`) and the admin dashboard
(`/admin`) on a **separate management port** by default, so they are not exposed
on the public application port (Spring Boot `management.server.port` parity).

| Key | Env var | Default | Meaning |
|---|---|---|---|
| `pyfly.server.port` | `PYFLY_SERVER_PORT` | `8080` | Application HTTP port (Spring `server.port`). |
| `pyfly.server.host` | `PYFLY_SERVER_HOST` | `0.0.0.0` | Application bind address (Spring `server.address`). |
| `pyfly.management.server.port` | `PYFLY_MANAGEMENT_SERVER_PORT` | `9090` | Management (actuator + admin) port. A different port runs a dedicated in-process listener; equal to the app port collapses to a single shared port; `-1` disables the management web endpoints entirely. |
| `pyfly.management.server.address` | `PYFLY_MANAGEMENT_SERVER_ADDRESS` | app host | Management bind address (e.g. `127.0.0.1` for node-local only). |
| `pyfly.management.server.base-path` | `PYFLY_MANAGEMENT_SERVER_BASE_PATH` | `""` | Path prefix on the management server. |

Out of the box: app on **`8080`**, management on **`9090`**. To run everything
on one port (the pre-`v26.06.102` behavior), set the management port equal to the
app port:

```bash
PYFLY_SERVER_PORT=8080 PYFLY_MANAGEMENT_SERVER_PORT=8080 python main.py
```

The management server is a second **in-process** listener (not extra workers);
it shares the same process, event loop and beans, and works regardless of the
ASGI server adapter (Uvicorn, Granian, Hypercorn). With `pyfly.server.workers > 1`
each worker binds the management port with `SO_REUSEPORT`; per-worker Prometheus
scraping uses `prometheus_client` multiprocess mode (`PROMETHEUS_MULTIPROC_DIR`),
the standard multi-process metrics approach.

> **Breaking change (v26.06.102):** the legacy `pyfly.web.port` / `pyfly.web.host`
> keys (and `PYFLY_WEB_PORT` / `PYFLY_WEB_HOST`) were removed in favor of
> `pyfly.server.port` / `pyfly.server.host`.

## Configuration Layering

PyFly's four-layer configuration system is the core of its flexibility. Each layer deeply
merges into the previous, with later layers taking precedence.

```
Priority (highest to lowest):

  4. Environment Variables        PYFLY_SERVER_PORT=9090
  3. Profile Overlay Files        pyfly-prod.yaml
  2. User Configuration File      pyfly.yaml
  1. Framework Defaults            pyfly-defaults.yaml (bundled)
```

### Layer 1: Framework Defaults

The bundled `pyfly-defaults.yaml` inside `pyfly.resources` provides sensible defaults for
every configuration key the framework reads. You never edit this file. It is loaded using
`importlib.resources` so it works correctly in packaged distributions. Its full contents
are listed in the [Framework Defaults Reference](#framework-defaults-reference).

### Layer 2: User Configuration File

Your `pyfly.yaml` or `pyfly.toml`. When no explicit path is given to `PyFlyApplication`,
it auto-discovers by checking these candidates in order:

1. `pyfly.yaml`
2. `pyfly.toml`
3. `config/pyfly.yaml`
4. `config/pyfly.toml`

### Layer 3: Profile Overlays

For each active profile, the corresponding overlay file is loaded and merged. If multiple
profiles are active, they are applied in order:

```bash
PYFLY_PROFILES_ACTIVE=dev,metrics
```

Merge order: defaults -> base -> `pyfly-dev.yaml` -> `pyfly-metrics.yaml`.

### Layer 4: Environment Variables

Checked at **read time** in `Config.get()`. This means they always win, even if set after
the config file is loaded. This layer enables runtime overrides without touching any
config files -- ideal for container deployments and CI/CD.

### Deep Merge Behavior

Layers are combined using a recursive deep merge (`Config._deep_merge()`). For nested
dictionaries, keys from the override layer are merged into the base; for non-dict values,
the override replaces the base entirely.

Example:

```yaml
# Base (pyfly.yaml)
pyfly:
  web:
    port: 8080
    host: "0.0.0.0"
    docs:
      enabled: true

# Overlay (pyfly-prod.yaml)
pyfly:
  web:
    port: 443
```

Result after merge:

```yaml
pyfly:
  web:
    port: 443           # overridden
    host: "0.0.0.0"     # preserved from base
    docs:
      enabled: true     # preserved from base
```

---

## Remote Config Import (Config Server)

PyFly can import configuration from a remote [config server](config-server.md) at
bootstrap. When `pyfly.cloud.config.uri` (or the alias `pyfly.config.import`) is set
and `pyfly.cloud.config.enabled` is not `false`, `PyFlyApplication._import_remote_config()`
fetches the remote bundle during construction and **deep-merges it as a high-precedence
source** on top of the locally loaded config.

```yaml
pyfly:
  cloud:
    config:
      uri: "http://config:8888"   # remote config server base URL
      enabled: true               # default true; set false to disable import
      label: "main"               # optional, defaults to "main"
      fail-fast: false            # default false; see below
      username: "configuser"      # optional HTTP basic auth
      password: "s3cret"          # optional HTTP basic auth
```

Behavior:

- The application name (`pyfly.app.name`, falling back to the app's own name) and
  the comma-joined active profiles are sent to the server's
  `/{application}/{profile}/{label}` endpoint via `ConfigClient`.
- The returned `propertySources` are flattened and merged into the live `Config`, and a
  `config-server (<uri>)` entry is appended to `loaded_sources`.
- The import is **non-fatal by default**: an unreachable server, a missing `httpx`
  dependency, or a non-200 response logs a warning and the app falls back to local config
  only. Set `pyfly.cloud.config.fail-fast: true` to make any import failure abort startup.
- Because the import runs in the synchronous `__init__`, it is skipped (with a warning) if
  an event loop is already running.

See the [Config Server guide](config-server.md) for the server side.

---

## Environment Variable Overrides

### Naming Convention

Every dot-notation config key maps to an environment variable:

1. Strip the `pyfly.` prefix (if present).
2. Replace `.` and `-` with `_`.
3. Uppercase.
4. Prefix with `PYFLY_`.

| Config Key | Environment Variable |
|---|---|
| `pyfly.app.name` | `PYFLY_APP_NAME` |
| `pyfly.server.port` | `PYFLY_SERVER_PORT` |
| `pyfly.management.server.port` | `PYFLY_MANAGEMENT_SERVER_PORT` |
| `pyfly.web.debug` | `PYFLY_WEB_DEBUG` |
| `pyfly.data.pool-size` | `PYFLY_DATA_POOL_SIZE` |
| `pyfly.cache.redis.url` | `PYFLY_CACHE_REDIS_URL` |
| `pyfly.client.retry.max-attempts` | `PYFLY_CLIENT_RETRY_MAX_ATTEMPTS` |
| `pyfly.logging.level.root` | `PYFLY_LOGGING_LEVEL_ROOT` |

### Type Coercion

Environment variables are always strings. When read via `Config.get()`, they are returned
as strings. Type coercion happens in `Config.bind()` when binding to a `@config_properties`
dataclass:

| Target Type | Coercion |
|---|---|
| `int` | `int(value)` |
| `float` | `float(value)` |
| `bool` | `value.lower() in ("true", "1", "yes")` |
| `str` | No coercion needed. |

### Environment Variable Examples

```bash
# Application server port (default 8080)
PYFLY_SERVER_PORT=8080
# Management (actuator + admin) port (default 9090)
PYFLY_MANAGEMENT_SERVER_PORT=9090

# Enable debug mode
PYFLY_WEB_DEBUG=true

# Set the database URL
PYFLY_DATA_URL="postgresql+asyncpg://prod:5432/mydb"

# Activate profiles
PYFLY_PROFILES_ACTIVE=prod,metrics

# Set cache TTL
PYFLY_CACHE_TTL=600

# Set retry attempts
PYFLY_CLIENT_RETRY_MAX_ATTEMPTS=5
```

---

## @config_properties

`@config_properties` creates typed configuration classes that bind to specific config
prefixes. This eliminates string-based config access and gives you IDE autocompletion,
type checking, and default values.

```python
from pyfly.core import config_properties
```

### Defining a Config Class

Decorate a `@dataclass` with `@config_properties(prefix="...")`:

```python
from dataclasses import dataclass
from pyfly.core import config_properties

@config_properties(prefix="pyfly.data")
@dataclass
class DataConfig:
    enabled: bool = False
    url: str = "sqlite+aiosqlite:///pyfly.db"
    echo: bool = False
    pool_size: int = 5
```

The `prefix` determines which config section is read. Field names must match the keys
in that section. The decorator sets `__pyfly_config_prefix__` on the class.

### Binding at Runtime

Call `config.bind(ConfigClass)` to produce a populated instance:

```python
config = Config.from_file("pyfly.yaml")
data_config = config.bind(DataConfig)

print(data_config.url)        # From pyfly.yaml or env var
print(data_config.pool_size)  # 5 (default) or overridden
```

If the class is not decorated with `@config_properties`, `bind()` raises a `ValueError`.

### How bind() Works Internally

1. Read the `__pyfly_config_prefix__` attribute from the class.
2. Call `effective_section(prefix)` — a resolved copy of the subtree with `${...}`
   placeholders expanded, environment-variable overrides applied, and env-only keys
   injected (values that exist only as `PYFLY_*` env vars but have no file entry).
3. For Pydantic `BaseModel` subclasses: pass the normalized section to
   `model_validate()` for fail-fast validation and rich type coercion.
4. For dataclasses: get type hints via `get_type_hints()`, match fields using relaxed
   (kebab/snake interchangeable) key lookup, apply type coercion as needed.
5. Construct the dataclass with the gathered kwargs. Fields not present in config
   use their dataclass default values.

### Type Coercion in bind()

When values come from a YAML file, they are already correctly typed (YAML parsers
handle int, float, bool natively). When values come from config sections that contain
string data (e.g., from environment variable injection), `bind()` coerces:

| Target Type | String Coercion Rule |
|---|---|
| `int` | `int(value)` |
| `float` | `float(value)` |
| `bool` | `value.lower() in ("true", "1", "yes")` |

Fields not present in the config section use the dataclass default values.

### Env-Only Keys (No File Leaf)

`bind()` resolves its section via `effective_section()`, which not only resolves
`${...}` placeholders and overlays env-var overrides, but also **injects env-only
keys** that have no corresponding leaf in any config file. A `PYFLY_<PREFIX>_*`
variable whose key does not exist in the loaded YAML/TOML is added to the bound section
so that `bind()` sees the same value `get()` would.

For example, with no `pyfly.data.replica-url` key anywhere in your files:

```bash
PYFLY_DATA_REPLICA_URL="postgresql+asyncpg://replica:5432/orders"
```

binds to a `replica_url` field on a `@config_properties(prefix="pyfly.data")` class.
The env suffix is split on `_` (treated Spring-style as path separators relative to the
prefix); only *absent* leaves are added, and existing file/overlay values are never
overwritten. This is implemented by `_inject_env_only()` in `core/config.py`.

---

## @Value (Field-Level Config Injection)

While `@config_properties` binds an entire configuration section to a dataclass, `@Value` injects individual configuration values directly into bean fields. It works as a Python descriptor that resolves expressions at bean creation time.

```python
from pyfly.core.value import Value
```

### Expression Syntax

`@Value` supports four expression forms:

| Expression | Behaviour | Example |
|---|---|---|
| `${key}` | Resolve from Config; raise `KeyError` if missing | `Value("${pyfly.app.name}")` |
| `${key:default}` | Resolve from Config; use default if missing | `Value("${pyfly.timeout:30}")` |
| `#{ ... }` | Evaluate a SpEL-lite expression (arithmetic/boolean/ternary, `${key}` substitution, `env`) | `Value("#{${pyfly.workers:1} * 2}")` |
| `literal` | Return the string as-is (no `${}`/`#{}` wrapper) | `Value("hello")` |

The `#{ ... }` form is the [SpEL-lite expression](#spel-lite-expressions) language described below.

The key uses dot-notation to navigate the Config hierarchy (e.g., `pyfly.data.mongodb.uri` resolves to `config["pyfly"]["data"]["mongodb"]["uri"]`).

Placeholder config-references use the same **relaxed** segment matching as `get()`:
kebab/snake-case segments are interchangeable. So `${my-prop.sub-key}` resolves a value
stored under `my_prop.sub_key` (and vice versa). Each `${...}` reference is also checked
against environment variables first — both its literal dotted name and the `PYFLY_*`
relaxed mapping (so `${app.name}` honors `PYFLY_APP_NAME`) — before falling back to the
config tree and finally the inline `:default`.

### Usage in Beans

Declare `Value` descriptors as class-level fields on any bean:

```python
from pyfly.container import service
from pyfly.core.value import Value


@service
class NotificationService:
    app_name: str = Value("${pyfly.app.name}")
    max_retries: int = Value("${notifications.max-retries:3}")
    sender_email: str = Value("${notifications.sender:noreply@example.com}")

    async def send(self, to: str, message: str) -> None:
        # self.app_name, self.max_retries, self.sender_email
        # are resolved from Config when the bean is created
        ...
```

The DI container resolves `Value` descriptors during bean initialization, before `@post_construct` hooks run.

### @Value vs @config_properties

| Feature | `@Value` | `@config_properties` |
|---|---|---|
| Granularity | Individual fields | Entire config section |
| Location | Any bean class | Dedicated config dataclass |
| Default values | Inline `${key:default}` | Dataclass field defaults |
| Type coercion | Manual (values returned as strings) | Automatic via `bind()` |
| Use case | A few scattered config values | Structured config with many related fields |

**Rule of thumb:** Use `@Value` for 1-3 config values in a bean. Use `@config_properties` when a component needs a whole section of related configuration.

Source file: `src/pyfly/core/value.py`

---

## SpEL-lite Expressions

PyFly ships a small, **safe** expression evaluator — its subset of Spring's SpEL. It
backs the `#{ ... }` form used by [`@Value`](#value-field-level-config-injection) and
[`@conditional_on_expression`](#conditional_on_expression), letting you compute config
values and toggle beans from arithmetic and config-placeholder substitution without
writing any Python at the call site.

```python
from pyfly.core.expression import evaluate, is_expression
```

### The `#{ ... }` Form

An expression is any string wrapped in `#{ ... }`. `evaluate(text, config=None)` parses
it with Python's `ast` module and evaluates it against a whitelist of node types; the
result is the computed value (any Python type).

Supported constructs:

| Category | Operators / Forms | Example | Result |
|---|---|---|---|
| Arithmetic | `+`, `-`, `*`, `/`, `//`, `%`, `**`, unary `+`/`-` | `#{2 * 5 + 1}` | `11` |
| Comparison | `==`, `!=`, `<`, `<=`, `>`, `>=` (incl. chained) | `#{3 > 2}` | `True` |
| Boolean | `and`, `or`, `not` | `#{not false}` | `True` |
| Ternary | `a if cond else b` | `#{100 if 2 > 1 else 200}` | `100` |
| Literals | numbers, strings, `true`/`false`/`null`, lists, tuples | `#{[1, 2, 3]}` | `[1, 2, 3]` |
| Subscript | `mapping[key]` | `#{env['HOME']}` | env value |

`true`/`false`/`null` (and their Python `True`/`False`/`None` spellings) are recognized as
literals.

**`${key:default}` substitution.** Before evaluation, every `${key}` /
`${key:default}` placeholder in the expression is resolved against the `Config` passed to
`evaluate()` and inlined as a literal. A bare `${key}` raises `ExpressionError` if the key
is missing; the `:default` form falls back to the default.

```python
from pyfly.core import Config
from pyfly.core.expression import evaluate

cfg = Config({"pyfly": {"workers": 4}})

evaluate("#{${pyfly.workers} * 2}", cfg)     # 8
evaluate("#{${pyfly.missing:3} + 1}", cfg)   # 4  (default used)
```

**The `env` mapping.** A read-only `env` mapping exposes the process environment
(`os.environ`) for subscripting:

```python
import os
os.environ["FEATURE_FLAG"] = "on"

evaluate("#{env['FEATURE_FLAG'] == 'on'}")   # True
```

In a bean, the same forms work through `@Value`:

```python
from pyfly.container import service
from pyfly.core.value import Value


@service
class PoolService:
    # double the configured worker count, defaulting to 1
    pool_size: int = Value("#{${pyfly.workers:1} * 2}")
    # enable batching only when more than one worker is configured
    batching: bool = Value("#{${pyfly.workers:1} > 1}")
```

`is_expression(text)` returns whether a string is a `#{ ... }` expression — `@Value`
uses it to decide between SpEL-lite evaluation and plain `${...}` placeholder resolution.

### @conditional_on_expression

`@conditional_on_expression` registers a bean only when a SpEL-lite `#{ ... }` expression
is truthy — the equivalent of Spring Boot's `@ConditionalOnExpression`. The expression is
evaluated against the active config at `ApplicationContext` startup.

```python
from pyfly.container import service
from pyfly.context import conditional_on_expression


@conditional_on_expression("#{${pyfly.workers:1} > 1}")
@service
class ParallelScheduler:
    """Only registered when pyfly.workers is greater than 1."""
    ...
```

Because the expression supports `${key:default}` substitution and the `env` mapping, it
covers numeric thresholds and environment-driven toggles that `@conditional_on_property`
(string equality only) cannot express. Combine it with the other `@conditional_on_*`
decorators (`@conditional_on_property`, `@conditional_on_class`, `@conditional_on_bean`,
`@conditional_on_missing_bean`) for auto-configuration-style wiring.

### Safety Model

The evaluator is intentionally **not** full SpEL — it is designed so an expression can
never execute arbitrary code:

- **No `eval`.** Expressions are parsed with `ast.parse(..., mode="eval")` and walked
  node-by-node; Python's `eval`/`exec` are never called.
- **Whitelisted node types only.** Only the operators and literal forms listed above are
  evaluated. Any other node raises `ExpressionError`.
- **No attribute access.** `#{(1).__class__}` is rejected — attribute navigation is not a
  whitelisted node.
- **No function or method calls.** `#{__import__('os')}` is rejected — call nodes are not
  whitelisted.
- **No assignment, no name resolution beyond the safe builtins.** The only names available
  are the literals (`true`/`false`/`null`) and the `env` mapping; an unknown name raises
  `ExpressionError`.

A malformed expression raises `ExpressionError` (a subclass of `PyFlyException`).

```python
from pyfly.core.expression import ExpressionError, evaluate

for unsafe in ("#{__import__('os')}", "#{(1).__class__}", "#{unknown_name}"):
    try:
        evaluate(unsafe)
    except ExpressionError:
        pass  # all three are rejected
```

Source file: `src/pyfly/core/expression.py` (decorator in `src/pyfly/context/conditions.py`)

---

## Framework Defaults Reference

The following are all default values from `pyfly-defaults.yaml`, organized by section.
Every key can be overridden in your config file or via environment variables.

### Application Defaults

| Key | Default | Description |
|---|---|---|
| `pyfly.app.name` | `"pyfly-app"` | Application name used in logs and the banner. |
| `pyfly.app.version` | `"0.1.0"` | Application version string. |
| `pyfly.app.description` | `""` | Human-readable application description. |

### Profiles Defaults

| Key | Default | Description |
|---|---|---|
| `pyfly.profiles.active` | `""` | Comma-separated list of active profiles. |

### Banner Defaults

| Key | Default | Description |
|---|---|---|
| `pyfly.banner.mode` | `"TEXT"` | Banner mode: `TEXT`, `MINIMAL`, or `OFF`. |
| `pyfly.banner.location` | `""` | Path to a custom banner file. Empty = use default ASCII art. |

### Logging Defaults

| Key | Default | Description |
|---|---|---|
| `pyfly.logging.level.root` | `"INFO"` | Root log level. |
| `pyfly.logging.format` | `"console"` | Log output format. |

### Web Defaults

| Key | Default | Description |
|---|---|---|
| `pyfly.server.port` | `8080` | Application HTTP port (Spring `server.port` parity). |
| `pyfly.server.host` | `"0.0.0.0"` | Application bind address (Spring `server.address` parity). |
| `pyfly.management.server.port` | `9090` | Management (actuator + admin) port. Equal to the app port = shared; `-1` = disabled. |
| `pyfly.management.server.address` | `null` | Management bind address (defaults to the app host). |
| `pyfly.web.debug` | `false` | Enable debug mode. |
| `pyfly.web.docs.enabled` | `true` | Enable API documentation endpoints. |
| `pyfly.web.actuator.enabled` | `false` | Enable actuator management endpoints. |

### Data Defaults

| Key | Default | Description |
|---|---|---|
| `pyfly.data.enabled` | `false` | Enable the data layer. |
| `pyfly.data.url` | `"sqlite+aiosqlite:///pyfly.db"` | Database connection URL. |
| `pyfly.data.echo` | `false` | Echo SQL statements (for debugging). |
| `pyfly.data.pool-size` | `5` | Connection pool size. |

### Cache Defaults

| Key | Default | Description |
|---|---|---|
| `pyfly.cache.enabled` | `false` | Enable caching. |
| `pyfly.cache.provider` | `"memory"` | Cache provider: `redis` or `memory`. |
| `pyfly.cache.redis.url` | `"redis://localhost:6379/0"` | Redis connection URL. |
| `pyfly.cache.ttl` | `300` | Default cache TTL in seconds. |

### Messaging Defaults

| Key | Default | Description |
|---|---|---|
| `pyfly.messaging.provider` | `"memory"` | Messaging provider: `kafka`, `rabbitmq`, or `memory`. |
| `pyfly.messaging.kafka.bootstrap-servers` | `"localhost:9092"` | Kafka bootstrap servers. |
| `pyfly.messaging.rabbitmq.url` | `"amqp://guest:guest@localhost/"` | RabbitMQ connection URL. |

### Client Defaults

| Key | Default | Description |
|---|---|---|
| `pyfly.client.timeout` | `30` | HTTP client timeout in seconds. |
| `pyfly.client.retry.max-attempts` | `3` | Maximum retry attempts. |
| `pyfly.client.retry.base-delay` | `1.0` | Base delay between retries (seconds). |
| `pyfly.client.circuit-breaker.failure-threshold` | `5` | Failures before the circuit opens. |
| `pyfly.client.circuit-breaker.recovery-timeout` | `30` | Seconds before attempting recovery. |

### Server Defaults

| Key | Default | Description |
|---|---|---|
| `pyfly.server.type` | `"auto"` | Server type: `auto`, `granian`, `uvicorn`, or `hypercorn`. |
| `pyfly.server.event-loop` | `"auto"` | Event loop: `auto`, `uvloop`, `winloop`, or `asyncio`. |
| `pyfly.server.workers` | `0` | Number of worker processes (0 = CPU count). |
| `pyfly.server.backlog` | `1024` | TCP connection backlog. |
| `pyfly.server.graceful-timeout` | `30` | Graceful shutdown timeout in seconds. |
| `pyfly.server.http` | `"auto"` | HTTP implementation: `auto`, `h11`, `httptools`. |
| `pyfly.server.keep-alive-timeout` | `5` | Keep-alive timeout in seconds. |

### Admin Defaults

| Key | Default | Description |
|---|---|---|
| `pyfly.admin.enabled` | `true` | Enable the admin dashboard. |
| `pyfly.admin.path` | `"/admin"` | URL path for the admin dashboard. |
| `pyfly.admin.title` | `"PyFly Admin"` | Dashboard title. |
| `pyfly.admin.theme` | `"auto"` | Theme: `auto`, `light`, or `dark`. |
| `pyfly.admin.require-auth` | `false` | Require authentication for admin access. |
| `pyfly.admin.refresh-interval` | `5000` | SSE refresh interval in milliseconds. |

### Security Defaults

| Key | Default | Description |
|---|---|---|
| `pyfly.security.enabled` | `false` | Enable the security module. |
| `pyfly.security.jwt.secret` | `"change-me-in-production"` | JWT signing secret. **Must be changed in production.** |
| `pyfly.security.jwt.algorithm` | `"HS256"` | JWT signing algorithm. |
| `pyfly.security.password.bcrypt-rounds` | `12` | Bcrypt hashing rounds. |

### Observability Defaults

| Key | Default | Description |
|---|---|---|
| `pyfly.observability.metrics.enabled` | `true` | Enable metrics collection. |
| `pyfly.observability.tracing.enabled` | `true` | Enable distributed tracing. |
| `pyfly.observability.tracing.service-name` | `"${pyfly.app.name}"` | OpenTelemetry service name (defaults to app name). |

### Full YAML Reference

```yaml
pyfly:
  app:
    name: "pyfly-app"
    version: "0.1.0"
    description: ""
  profiles:
    active: ""
  banner:
    mode: "TEXT"
    location: ""
  logging:
    level:
      root: "INFO"
    format: "console"
  web:
    port: 8080
    host: "0.0.0.0"
    debug: false
    docs:
      enabled: true
    actuator:
      enabled: false
  server:
    type: "auto"
    event-loop: "auto"
    workers: 0
    backlog: 1024
    graceful-timeout: 30
    http: "auto"
    keep-alive-timeout: 5
    granian:
      runtime-threads: 1
      runtime-mode: "auto"
      respawn-failed-workers: true
  data:
    enabled: false
    url: "sqlite+aiosqlite:///pyfly.db"
    echo: false
    pool-size: 5
    relational:
      ddl-auto: "create"
  cache:
    enabled: false
    provider: "memory"
    ttl: 300
  messaging:
    provider: "memory"
  client:
    timeout: 30
    retry:
      max-attempts: 3
      base-delay: 1.0
    circuit-breaker:
      failure-threshold: 5
      recovery-timeout: 30
  admin:
    enabled: true
    path: "/admin"
    title: "PyFly Admin"
    theme: "auto"
    require-auth: false
    refresh-interval: 5000
  security:
    enabled: false
    jwt:
      secret: "change-me-in-production"
      algorithm: "HS256"
    password:
      bcrypt-rounds: 12
  observability:
    metrics:
      enabled: true
    tracing:
      enabled: true
      service-name: "${pyfly.app.name}"
  transactional:
    enabled: false
    saga:
      compensation_policy: STRICT_SEQUENTIAL
      default_timeout_ms: 300000
    tcc:
      default_timeout_ms: 30000
      retry_enabled: true
      max_retries: 3
```

---

## Complete Example: Multi-Environment Setup

This example demonstrates a realistic multi-environment configuration setup for a service
that uses a database, cache, and messaging.

### Project Structure

```
order-service/
  pyfly.yaml            # Base config (shared)
  pyfly-dev.yaml        # Dev overrides
  pyfly-staging.yaml    # Staging overrides
  pyfly-prod.yaml       # Production overrides
  order_service/
    __init__.py
    app.py
    config.py
    ...
  main.py
```

### pyfly.yaml (Base)

```yaml
pyfly:
  app:
    name: "order-service"
    version: "3.2.0"

  web:
    port: 8080
    docs:
      enabled: true

  data:
    enabled: true
    url: "sqlite+aiosqlite:///orders.db"
    pool-size: 5

  cache:
    enabled: true
    provider: "memory"
    ttl: 300

  messaging:
    provider: "memory"

  logging:
    level:
      root: "INFO"
    format: "console"
```

### pyfly-dev.yaml

```yaml
pyfly:
  web:
    debug: true
  data:
    echo: true
  logging:
    level:
      root: "DEBUG"
```

### pyfly-staging.yaml

```yaml
pyfly:
  web:
    port: 8080
  data:
    url: "postgresql+asyncpg://staging-db:5432/orders"
    pool-size: 10
  cache:
    redis:
      url: "redis://staging-redis:6379/0"
```

### pyfly-prod.yaml

```yaml
pyfly:
  web:
    port: 443
    debug: false
    docs:
      enabled: false
  data:
    url: "postgresql+asyncpg://prod-db:5432/orders"
    pool-size: 25
  cache:
    redis:
      url: "redis://prod-redis:6379/0"
    ttl: 600
  messaging:
    kafka:
      bootstrap-servers: "kafka-1:9092,kafka-2:9092,kafka-3:9092"
  logging:
    level:
      root: "WARNING"
    format: "json"
  banner:
    mode: "OFF"
```

### config.py (Typed Config)

```python
from dataclasses import dataclass
from pyfly.core import config_properties

@config_properties(prefix="pyfly.data")
@dataclass
class DataConfig:
    enabled: bool = False
    url: str = "sqlite+aiosqlite:///orders.db"
    echo: bool = False
    pool_size: int = 5

@config_properties(prefix="pyfly.cache")
@dataclass
class CacheConfig:
    enabled: bool = False
    provider: str = "memory"
    ttl: int = 300
```

### app.py

```python
from pyfly.core import pyfly_application

@pyfly_application(
    name="order-service",
    version="3.2.0",
    scan_packages=["order_service"],
)
class OrderServiceApp:
    pass
```

### main.py

```python
import asyncio
from pyfly.core import PyFlyApplication
from order_service.app import OrderServiceApp
from order_service.config import DataConfig, CacheConfig

async def main():
    app = PyFlyApplication(OrderServiceApp)

    # Access typed config
    data_cfg = app.config.bind(DataConfig)
    cache_cfg = app.config.bind(CacheConfig)

    print(f"Database: {data_cfg.url} (pool={data_cfg.pool_size})")
    print(f"Cache TTL: {cache_cfg.ttl}s")

    await app.startup()
    # ... serve requests ...
    await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
```

### Running

```bash
# Development (local SQLite, debug logging)
PYFLY_PROFILES_ACTIVE=dev python main.py

# Staging (PostgreSQL, Redis cache)
PYFLY_PROFILES_ACTIVE=staging python main.py

# Production (full infrastructure, JSON logging, no docs)
PYFLY_PROFILES_ACTIVE=prod python main.py

# Production with env var overrides (e.g., in a container)
PYFLY_PROFILES_ACTIVE=prod \
  PYFLY_DATA_URL="postgresql+asyncpg://rds-prod:5432/orders" \
  PYFLY_SERVER_PORT=8080 \
  python main.py
```

### Understanding the Layering

For the production container example above, the effective configuration is built as:

1. **Framework defaults** (from `pyfly-defaults.yaml`)
2. **Base config** (`pyfly.yaml`: pool-size=5, port=8080, cache TTL=300)
3. **Profile overlay** (`pyfly-prod.yaml`: pool-size=25, port=443, cache TTL=600)
4. **Env vars** (`PYFLY_DATA_URL` overrides the prod DB URL, `PYFLY_SERVER_PORT=8080` overrides the prod port)

Final effective values:

| Key | Value | Source |
|---|---|---|
| `pyfly.server.port` | `"8080"` | Env var (overrides prod overlay's 443) |
| `pyfly.web.debug` | `false` | Prod overlay |
| `pyfly.web.docs.enabled` | `false` | Prod overlay |
| `pyfly.data.url` | `"postgresql+asyncpg://rds-prod:5432/orders"` | Env var |
| `pyfly.data.pool-size` | `25` | Prod overlay |
| `pyfly.cache.ttl` | `600` | Prod overlay |
| `pyfly.logging.format` | `"json"` | Prod overlay |
| `pyfly.logging.level.root` | `"WARNING"` | Prod overlay |
| `pyfly.banner.mode` | `"OFF"` | Prod overlay |
