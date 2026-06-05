# Core & Lifecycle Guide

This guide covers the foundational building blocks of every PyFly application: the application
entry-point decorator, the bootstrap class, the configuration system, and the startup banner.
Understanding these components is essential because every other module in the framework
depends on them.

---

## Table of Contents

1. [Introduction](#introduction)
2. [The @pyfly_application Decorator](#the-pyfly_application-decorator)
3. [PyFlyApplication Class](#pyflyapplication-class)
   - [Constructor](#constructor)
   - [Startup Sequence](#startup-sequence)
   - [startup() Method](#startup-method)
   - [shutdown() Method](#shutdown-method)
   - [Properties](#properties)
4. [Configuration System](#configuration-system)
   - [Config Class](#config-class)
   - [Loading Configuration: from_file()](#loading-configuration-from_file)
   - [Reading Values: get()](#reading-values-get)
   - [Reading Sections: get_section()](#reading-sections-get_section)
   - [Typed Binding: bind()](#typed-binding-bind)
5. [@config_properties Decorator](#config_properties-decorator)
6. [Configuration Layering](#configuration-layering)
   - [Framework Defaults](#framework-defaults)
   - [User Configuration File](#user-configuration-file)
   - [Profile Overlays](#profile-overlays)
   - [Environment Variable Overrides](#environment-variable-overrides)
7. [YAML and TOML Support](#yaml-and-toml-support)
8. [Startup Banner](#startup-banner)
   - [BannerMode Enum](#bannermode-enum)
   - [BannerPrinter Class](#bannerprinter-class)
   - [Custom Banner Files](#custom-banner-files)
   - [Placeholders](#placeholders)
9. [Framework Defaults Reference](#framework-defaults-reference)
10. [Complete Example](#complete-example)

---

## Introduction

The `pyfly.core` module provides three concerns that every application needs:

| Concern | Classes / Functions | Purpose |
|---|---|---|
| **Bootstrap** | `@pyfly_application`, `PyFlyApplication` | Mark an entry-point class and orchestrate the startup/shutdown lifecycle. |
| **Configuration** | `Config`, `@config_properties`, `Value` | Load, layer, and access configuration from YAML/TOML files, profiles, and environment variables. |
| **Banner** | `BannerMode`, `BannerPrinter` | Render a startup banner to stdout (ASCII art, minimal one-liner, or off). |
| **Lifecycle** | `Lifecycle` protocol | Unified `start()`/`stop()` contract for all infrastructure adapters. |
| **Logging Fallback** | `StdlibLoggingAdapter` | Zero-dependency fallback when `structlog` is not installed. Wraps stdlib `logging` with structlog-style key-value API. |

All public symbols are re-exported from `pyfly.core`:

```python
from pyfly.core import (
    PyFlyApplication,
    pyfly_application,
    Config,
    config_properties,
    BannerMode,
    BannerPrinter,
    Value,
)
from pyfly.kernel import Lifecycle
```

---

## The @pyfly_application Decorator

`@pyfly_application` marks a plain Python class as the entry point of a PyFly application.
It does **not** modify the class behavior at runtime; instead it attaches metadata attributes
that `PyFlyApplication` reads during bootstrap.

### Signature

```python
def pyfly_application(
    name: str,
    version: str = "0.1.0",
    scan_packages: list[str] | None = None,
    description: str = "",
) -> Any:
```

### Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `name` | `str` | *(required)* | Logical application name, used in log messages and the banner. |
| `version` | `str` | `"0.1.0"` | Application version string. |
| `scan_packages` | `list[str] \| None` | `None` | Dotted package names to scan for stereotype-decorated classes (e.g. `["myapp.services", "myapp.controllers"]`). Each package is recursively walked. |
| `description` | `str` | `""` | Human-readable description of the application. |

### What it does

The decorator stores four hidden attributes on the decorated class:

| Attribute | Value |
|---|---|
| `__pyfly_app_name__` | The `name` argument |
| `__pyfly_app_version__` | The `version` argument |
| `__pyfly_scan_packages__` | The `scan_packages` list (defaults to `[]`) |
| `__pyfly_app_description__` | The `description` argument |

### Example

```python
@pyfly_application(
    name="order-service",
    version="2.1.0",
    scan_packages=["orders.domain", "orders.infra"],
    description="Manages customer orders",
)
class OrderServiceApp:
    pass
```

---

## PyFlyApplication Class

`PyFlyApplication` is the main bootstrap class. It reads the metadata from your
`@pyfly_application`-decorated class, loads configuration, sets up logging, creates the
`ApplicationContext`, and runs the full startup sequence.

### Constructor

```python
class PyFlyApplication:
    def __init__(
        self,
        app_class: type,
        config_path: str | Path | None = None,
    ) -> None:
```

| Parameter | Type | Description |
|---|---|---|
| `app_class` | `type` | The class decorated with `@pyfly_application`. |
| `config_path` | `str \| Path \| None` | Explicit path to a config file. When `None`, the framework auto-discovers by checking these candidates in order: `pyfly.yaml`, `pyfly.toml`, `config/pyfly.yaml`, `config/pyfly.toml`. |

When the constructor runs it performs these steps:

1. **Resolves the config file** -- either the explicit path or auto-discovery.
2. **Resolves active profiles early** -- reads `PYFLY_PROFILES_ACTIVE` env var, then falls back to `pyfly.profiles.active` inside the base config file.
3. **Loads configuration** via `Config.from_file()`, which merges framework defaults, the user config, and profile overlays.
4. **Configures structured logging** — uses `StructlogAdapter` when `structlog` is installed, or falls back to `StdlibLoggingAdapter` (zero-dependency stdlib `logging` wrapper) when it is not.
5. **Creates the `ApplicationContext`** (DI container, environment, event bus).
6. **Scans packages** listed in `scan_packages`, registering all discovered stereotype-decorated classes into the container.

### Startup Sequence

The startup sequence follows Spring Boot parity. The full ordered list (from the class
docstring):

1. Configure logging (from the `pyfly.yaml` logging section)
2. Print banner (respecting `pyfly.banner.mode`)
3. Log `"Starting {app} v{version}"`
4. Log `"Active profiles: {profiles}"` or `"No active profiles set"`
5. Log loaded configuration sources
6. Load profile-specific config files
7. Filter beans by active profiles
8. Sort beans by `@order` value
9. Initialize beans (respecting order)
10. Start infrastructure adapters (fail-fast validation)
11. Log `"Started {app} in {time}s ({count} beans initialized)"`
12. Log mapped endpoints and API documentation URLs

Steps 1, 6, and part of 7 happen in the constructor. Steps 2-5 and 7-12 happen in the
async `startup()` method.

### startup() Method

```python
async def startup(self) -> None:
```

This is the async entry point you call to bring the application to life. It:

1. Renders and prints the startup banner.
2. Logs the starting message with the app name and version.
3. Logs the active profiles (or a "no active profiles" fallback).
4. Logs loaded configuration sources (`config.loaded_sources`).
5. Logs deferred package scan results.
6. Calls `ApplicationContext.start()`, which handles profile filtering, condition evaluation, bean ordering, eager singleton resolution, lifecycle hooks (`@post_construct`), post-processors, event publishing (`ContextRefreshedEvent`, `ApplicationReadyEvent`), and adapter lifecycle. If any adapter fails to start, it logs the error and raises `BeanCreationException`.
7. Records startup time and logs the completion message.
8. Logs mapped routes and API documentation URLs after startup.

### shutdown() Method

```python
async def shutdown(self) -> None:
```

Logs a shutdown message and delegates to `ApplicationContext.stop()`, which calls
`@pre_destroy` on all resolved beans in reverse initialization order and publishes
`ContextClosedEvent`.

### Fail-Fast Startup

PyFly follows Spring Boot's fail-fast principle: if explicitly configured infrastructure
(database, cache, message broker) cannot be reached at startup, the application fails
immediately with a clear error rather than starting in a broken state.

When an infrastructure adapter's `start()` method fails (e.g., Redis is unreachable,
Kafka broker is down), the framework:

1. Catches the exception.
2. Wraps it in a `BeanCreationException` with the subsystem name and provider.
3. Logs a structured error: `application_failed app={name} error={detail} subsystem={subsystem} provider={provider}`.
4. Re-raises the exception, terminating startup.

```python
from pyfly.container.exceptions import BeanCreationException

try:
    await app.startup()
except BeanCreationException as e:
    print(f"Startup failed: {e.subsystem}/{e.provider}: {e.reason}")
```

This ensures you detect infrastructure problems at deploy time, not when the first
request hits a broken adapter at runtime.

### Properties

| Property | Type | Description |
|---|---|---|
| `context` | `ApplicationContext` | The fully initialized application context. |
| `startup_time_seconds` | `float` | Wall-clock seconds taken by `startup()`. |

---

## Configuration System

### Config Class

`Config` is the central configuration holder. It wraps a nested dictionary and provides
dot-notation access with environment variable overrides.

```python
class Config:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
```

Priority (highest wins):
1. Environment variables (`PYFLY_SECTION_KEY` format)
2. Configuration dict / YAML file values
3. Dataclass defaults (when using `bind()`)

### Loading Configuration: from_file()

```python
@classmethod
def from_file(
    cls,
    path: str | Path,
    active_profiles: list[str] | None = None,
    load_defaults: bool = True,
) -> Config:
```

Merge order (later wins):

1. **Framework defaults** (`pyfly-defaults.yaml` bundled inside `pyfly.resources`)
2. **Base config** (the file at `path`)
3. **Profile overlays** -- for each active profile, looks for `{stem}-{profile}{suffix}`
   in the same directory (e.g. `pyfly-dev.yaml`, `pyfly-prod.toml`)
4. **Environment variables** -- handled at read time in `get()`

| Parameter | Type | Description |
|---|---|---|
| `path` | `str \| Path` | Path to the base configuration file. |
| `active_profiles` | `list[str] \| None` | Profiles whose overlays should be merged. |
| `load_defaults` | `bool` | Whether to load the bundled framework defaults as the base layer. Defaults to `True`. |

### Loading Configuration: from_sources()

```python
@classmethod
def from_sources(
    cls,
    base_dir: str | Path,
    active_profiles: list[str] | None = None,
    load_defaults: bool = True,
) -> Config:
```

Multi-source configuration loading with source tracking. Unlike `from_file()` which
takes a single file path, `from_sources()` auto-discovers all config files in the
given directory:

1. Loads framework defaults (`pyfly-defaults.yaml`) — skipped if `load_defaults=False`
2. Discovers and loads `config/pyfly.yaml` or `config/pyfly.toml` (config subdirectory)
3. Discovers and loads `pyfly.yaml` or `pyfly.toml` (project root)
4. Loads profile overlay files for each active profile (from both locations)
5. Records which sources were loaded in `loaded_sources`

### Config Source Tracking: loaded_sources

```python
@property
def loaded_sources(self) -> list[str]:
```

Returns a list of human-readable strings describing which configuration sources were
loaded and in what order. Useful for debugging configuration issues:

```python
config = Config.from_sources(".", active_profiles=["prod"])
for source in config.loaded_sources:
    print(source)
# pyfly-defaults.yaml (framework defaults)
# pyfly.yaml
# pyfly-prod.yaml (profile: prod)
```

These sources are also logged during startup for visibility.

### Reading Values: get()

```python
def get(self, key: str, default: Any = None) -> Any:
```

Retrieves a value by dot-notation key. Environment variables are checked **first**.

**Dot-notation to env var mapping:**

The key is transformed as follows:
- If the key starts with `pyfly.`, that prefix is stripped before building the env var name.
- Remaining dots and hyphens become underscores.
- The result is uppercased and prefixed with `PYFLY_`.

Examples:

| Config Key | Environment Variable |
|---|---|
| `pyfly.app.name` | `PYFLY_APP_NAME` |
| `pyfly.web.port` | `PYFLY_WEB_PORT` |
| `pyfly.data.pool-size` | `PYFLY_DATA_POOL_SIZE` |
| `app.name` | `PYFLY_APP_NAME` |

If no env var is set, the method walks the nested dictionary using the dot-separated parts
and returns the found value or `default`.

### Reading Sections: get_section()

```python
def get_section(self, prefix: str) -> dict[str, Any]:
```

Returns all values under a prefix as a flat dictionary. Useful when you need an entire
config subtree.

```python
config.get_section("pyfly.web")
# Returns: {"port": 8080, "host": "0.0.0.0", "debug": False, "docs": {"enabled": True}, ...}
```

### Typed Binding: bind()

```python
def bind(self, config_cls: type[T]) -> T:
```

Binds configuration values to a `@config_properties` dataclass or Pydantic `BaseModel`.
It reads the prefix from the decorator, fetches the effective config section (with
`${...}` placeholders resolved, env overrides applied, and env-only keys injected),
and constructs the dataclass with relaxed key matching and type coercion.

---

## @config_properties Decorator

`@config_properties` marks a dataclass as bindable to a specific configuration prefix.

```python
def config_properties(prefix: str):
```

The decorator stores the prefix in the `__pyfly_config_prefix__` attribute on the class.

### Usage

```python
from dataclasses import dataclass
from pyfly.core import config_properties

@config_properties(prefix="pyfly.web")
@dataclass
class WebConfig:
    port: int = 8080
    host: str = "0.0.0.0"
    debug: bool = False
```

Then bind it from a `Config` instance:

```python
config = Config.from_file("pyfly.yaml")
web_config = config.bind(WebConfig)

print(web_config.port)   # 8080 (or whatever pyfly.yaml says)
print(web_config.debug)  # False
```

### Type Coercion

When values come from YAML (already typed) they are used directly. When values come from
environment variables (always strings), the `bind()` method coerces them:

| Target Type | Coercion Rule |
|---|---|
| `int` | `int(value)` |
| `float` | `float(value)` |
| `bool` | `value.lower() in ("true", "1", "yes")` |

---

### Pydantic Configuration Properties

In addition to dataclasses, `Config.bind()` supports Pydantic `BaseModel` subclasses for fail-fast validation with rich type coercion and constraint checking at startup.

```python
from pydantic import BaseModel, Field
from pyfly.core.config import config_properties

@config_properties(prefix="myapp.database")
class DatabaseProperties(BaseModel):
    url: str = "postgresql://localhost/mydb"
    pool_size: int = Field(default=5, ge=1, le=100)
    timeout: float = Field(default=30.0, gt=0)
    ssl: bool = False
```

When `Config.bind(DatabaseProperties)` is called:
- Pydantic's `model_validate()` handles type coercion, nested models, and constraint validation
- `ValidationError` is raised at startup if any field fails validation (fail-fast)
- Nested `BaseModel` fields are automatically constructed from nested config dicts
- All Pydantic validators (`@field_validator`, `@model_validator`) are supported

---

## Configuration Layering

PyFly uses a four-layer configuration system. Each layer deeply merges into the previous
one, with later layers winning on conflicts.

```
+-----------------------------------------------+
|  4. Environment Variables  (highest priority)  |  PYFLY_WEB_PORT=9090
+-----------------------------------------------+
|  3. Profile Overlay                            |  pyfly-prod.yaml
+-----------------------------------------------+
|  2. User Config File                           |  pyfly.yaml
+-----------------------------------------------+
|  1. Framework Defaults     (lowest priority)   |  pyfly-defaults.yaml
+-----------------------------------------------+
```

### Framework Defaults

Bundled inside `pyfly.resources/pyfly-defaults.yaml`. Provides sensible defaults for every
configuration key the framework reads. You never need to edit this file; override any value
in your own config file.

### User Configuration File

Your `pyfly.yaml` (or `pyfly.toml`) at the project root or in a `config/` directory. This
is the primary place to set application-specific configuration.

### Profile Overlays

For each active profile, PyFly looks for a file named `{stem}-{profile}{suffix}` in the
same directory as the base config file. For example, with `pyfly.yaml` as the base:

- Profile `dev` loads `pyfly-dev.yaml`
- Profile `prod` loads `pyfly-prod.yaml`

Profiles are activated by:

1. The `PYFLY_PROFILES_ACTIVE` environment variable (comma-separated)
2. The `pyfly.profiles.active` key in the base config file

### Environment Variable Overrides

Any config key can be overridden at runtime by setting the corresponding environment
variable. The naming convention is:

```
pyfly.data.pool-size  -->  PYFLY_DATA_POOL_SIZE
```

Environment variables are checked at **read time** (in `get()`), not at load time, so
they always take precedence.

---

## YAML and TOML Support

PyFly supports both YAML and TOML configuration files. The file format is determined by
the file extension (`.yaml` or `.toml`).

### YAML Example

```yaml
# pyfly.yaml
pyfly:
  app:
    name: "my-service"
    version: "1.0.0"
  web:
    port: 8080
    host: "0.0.0.0"
  data:
    enabled: true
    url: "postgresql+asyncpg://localhost/mydb"
```

### TOML Example

```toml
# pyfly.toml
[pyfly.app]
name = "my-service"
version = "1.0.0"

[pyfly.web]
port = 8080
host = "0.0.0.0"

[pyfly.data]
enabled = true
url = "postgresql+asyncpg://localhost/mydb"
```

Both formats produce the same nested dictionary structure and are interchangeable. Profile
overlays use the same suffix as the base file: if the base is `.toml`, profiles must also
be `.toml`.

---

## Startup Banner

PyFly prints a startup banner when the application starts. The banner system supports
three modes and custom banner files.

### BannerMode Enum

```python
class BannerMode(enum.Enum):
    TEXT = "TEXT"
    MINIMAL = "MINIMAL"
    OFF = "OFF"
```

| Mode | Behavior |
|---|---|
| `TEXT` | Full ASCII art banner (default) with a framework version line. |
| `MINIMAL` | Single line: `:: PyFly :: (v26.05.01)` |
| `OFF` | No banner output at all. |

### BannerPrinter Class

`BannerPrinter` renders the startup banner. It is typically created via the
`from_config()` class method:

```python
@classmethod
def from_config(
    cls,
    config: Config,
    version: str = "0.1.0",
    app_name: str = "",
    app_version: str = "",
    active_profiles: list[str] | None = None,
) -> BannerPrinter:
```

This reads `pyfly.banner.mode` and `pyfly.banner.location` from the config.

The `render()` method returns the banner as a string (or `""` if mode is `OFF`):

```python
banner = BannerPrinter.from_config(config, version="1.0.0", app_name="my-app")
print(banner.render())
```

### Default Banner

When no custom banner file is configured, the default ASCII art banner is displayed:

```
                _____.__
______ ___.__._/ ____\  | ___.__.
\____ <   |  |\   __\|  |<   |  |
|  |_> >___  | |  |  |  |_\___  |
|   __// ____| |__|  |____/ ____|
|__|   \/                 \/

:: PyFly Framework :: (v26.05.01)
```

### Custom Banner Files

Set `pyfly.banner.location` in your config to point to a text file:

```yaml
pyfly:
  banner:
    mode: "TEXT"
    location: "banner.txt"
```

The file is loaded and its contents replace the default ASCII art. Placeholders within
the file are substituted before rendering.

### Placeholders

Custom banner files (and the default banner) support these placeholders:

| Placeholder | Replaced With |
|---|---|
| `${pyfly.version}` | Framework version |
| `${app.name}` | Application name |
| `${app.version}` | Application version |
| `${profiles.active}` | Comma-separated active profiles |

Example custom `banner.txt`:

```
====================================
  ${app.name} v${app.version}
  Profiles: ${profiles.active}
  PyFly ${pyfly.version}
====================================
```

---

## Framework Defaults Reference

The following values are the built-in defaults from `pyfly-defaults.yaml`. Every key
can be overridden in your config file or via environment variables.

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

  data:
    enabled: false
    url: "sqlite+aiosqlite:///pyfly.db"
    echo: false
    pool-size: 5

  cache:
    enabled: false
    provider: "memory"
    redis:
      url: "redis://localhost:6379/0"
    ttl: 300

  messaging:
    provider: "memory"
    kafka:
      bootstrap-servers: "localhost:9092"
    rabbitmq:
      url: "amqp://guest:guest@localhost/"

  client:
    timeout: 30
    retry:
      max-attempts: 3
      base-delay: 1.0
    circuit-breaker:
      failure-threshold: 5
      recovery-timeout: 30
```

### Key Defaults at a Glance

| Key | Default | Description |
|---|---|---|
| `pyfly.app.name` | `"pyfly-app"` | Application name |
| `pyfly.web.port` | `8080` | HTTP server port |
| `pyfly.web.host` | `"0.0.0.0"` | HTTP server bind address |
| `pyfly.web.debug` | `false` | Debug mode |
| `pyfly.logging.level.root` | `"INFO"` | Root log level |
| `pyfly.logging.format` | `"console"` | Log output format |
| `pyfly.data.enabled` | `false` | Enable data layer |
| `pyfly.data.url` | `"sqlite+aiosqlite:///pyfly.db"` | Database URL |
| `pyfly.data.pool-size` | `5` | Connection pool size |
| `pyfly.cache.enabled` | `false` | Enable caching |
| `pyfly.cache.provider` | `"memory"` | Cache backend (`redis`, `memory`) |
| `pyfly.cache.ttl` | `300` | Default TTL in seconds |
| `pyfly.messaging.provider` | `"memory"` | Messaging backend (`kafka`, `rabbitmq`, `memory`) |
| `pyfly.client.timeout` | `30` | HTTP client timeout in seconds |
| `pyfly.client.retry.max-attempts` | `3` | Retry attempts |
| `pyfly.client.circuit-breaker.failure-threshold` | `5` | Circuit breaker failure threshold |

---

## Complete Example

Below is a full example that creates a PyFly application from scratch, with a custom
config file, typed config properties, and the complete startup/shutdown lifecycle.

### Project Structure

```
my-service/
  pyfly.yaml
  pyfly-prod.yaml
  banner.txt
  my_service/
    __init__.py
    app.py
    config.py
    services/
      __init__.py
      greeting_service.py
    controllers/
      __init__.py
      greeting_controller.py
  main.py
```

### pyfly.yaml

```yaml
pyfly:
  app:
    name: "greeting-service"
    version: "1.0.0"
  profiles:
    active: ""
  banner:
    mode: "TEXT"
    location: "banner.txt"
  web:
    port: 8080
  greeting:
    default-name: "World"
    max-length: 100
```

### pyfly-prod.yaml

```yaml
pyfly:
  web:
    port: 443
    debug: false
  logging:
    level:
      root: "WARNING"
  greeting:
    max-length: 50
```

### my_service/config.py

```python
from dataclasses import dataclass
from pyfly.core import config_properties

@config_properties(prefix="pyfly.greeting")
@dataclass
class GreetingConfig:
    default_name: str = "World"
    max_length: int = 100
```

### my_service/app.py

```python
from pyfly.core import pyfly_application

@pyfly_application(
    name="greeting-service",
    version="1.0.0",
    scan_packages=["my_service.services", "my_service.controllers"],
    description="A simple greeting microservice",
)
class GreetingApp:
    pass
```

### main.py

```python
import asyncio
from pyfly.core import PyFlyApplication
from my_service.app import GreetingApp

async def main():
    app = PyFlyApplication(GreetingApp)

    # Bind typed config
    from my_service.config import GreetingConfig
    greeting_config = app.config.bind(GreetingConfig)
    print(f"Default name: {greeting_config.default_name}")

    # Start the application
    await app.startup()
    print(f"Started in {app.startup_time_seconds:.3f}s")

    # Access beans via the context
    # service = app.context.get_bean(GreetingService)

    # ... run your application ...

    # Shutdown
    await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
```

### Running with a Profile

```bash
# Via environment variable
PYFLY_PROFILES_ACTIVE=prod python main.py

# Or set it in pyfly.yaml:
# pyfly:
#   profiles:
#     active: "prod"
```

### Overriding Config with Environment Variables

```bash
# Override the web port
PYFLY_WEB_PORT=9090 python main.py

# Override the greeting default name
PYFLY_GREETING_DEFAULT_NAME="PyFly" python main.py
```

This example demonstrates the full lifecycle: decorator metadata, config loading with
profiles and env var overrides, typed config properties, async startup and shutdown, and
the `ApplicationContext` integration. From here, you would typically add services,
repositories, and controllers using the [Dependency Injection](dependency-injection.md)
system.
