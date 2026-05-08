# Starters — Layered Bundles

Starters are **opinionated bundles** that activate every framework
module a given service tier needs in a single decorator. They mirror
``org.fireflyframework.starter.*`` (Java) and
``FireflyFramework.Starter.*`` (.NET) so a service that's been written
on one platform reads the same way on every other.

## Available starters

| Starter | Activates | Use it for |
|---------|-----------|-----------|
| **Core** (`enable_core_stack`) | web, server, observability, metrics, tracing, cache, EDA, CQRS, resilience, actuator (+ metrics), AOP | Any infra-tier service. The foundation every other starter pulls in. |
| **Web** (`enable_web_stack`) | web, server, observability, metrics, tracing, actuator (+ metrics), resilience | Pure HTTP/REST APIs that don't need EDA, CQRS or cache. |
| **Application** (`enable_application_stack`) | core stack + plugins, security (JWT + password), sessions, i18n, scheduling, transactional engine, IDP, callbacks, webhooks, notifications | Application/orchestration tier with auth, scheduling and integrations. |
| **Data** (`enable_data_stack`) | core stack + relational, document, HTTP client, scheduling, resilience | Data ingestion / enrichment / batch services. |
| **Domain** (`enable_domain_stack`) | core stack + event sourcing, transactional engine, rule engine, relational, HTTP client, plugins. Re-exports every `pyfly.domain` DDD primitive. | DDD-style domain microservices. |

## Two ways to use a starter

### 1. Declarative (preferred) — decorate the application class

```python
from pyfly.core import pyfly_application
from pyfly.starters.domain import enable_domain_stack

@enable_domain_stack
@pyfly_application(name="my-service", scan_packages=["my_service"])
class Application:
    pass
```

`PyFlyApplication.__init__` sees the `__pyfly_starter_*__` attributes,
expands every dotted key into the nested config dictionary, and
**merges the result between framework defaults and the user's
`pyfly.yaml`**. So:

- Framework default `pyfly.cqrs.enabled=false` → starter sets `true`.
- User `pyfly.yaml` says `pyfly.cqrs.enabled=false` → user wins
  (explicit user choice always beats the bundle).

### 2. Imperative (explicit) — call `register_*_stack(app)`

This mirrors .NET's `services.AddFireflyCore(...)`:

```python
from pyfly.core.application import PyFlyApplication
from pyfly.starters.core import register_core_stack

@pyfly_application(name="my-service", scan_packages=["my_service"])
class Application: pass

app = PyFlyApplication(Application)
register_core_stack(app)   # explicit registration — overrides config files
await app.startup()
```

Imperative registration is **authoritative**: starter values win over
anything already in the config (including a user `pyfly.yaml`). The
last `register_*_stack(...)` call wins for a given key, matching .NET's
`services.AddX(...)` semantics.

## Re-exports — single import line per layer

Each starter re-exports the most commonly used types and decorators of
its tier so a controller / service file needs only one import line.

### Core layer

```python
from pyfly.starters.core import (
    Autowired, Scope, component, configuration, rest_controller, service,
    Command, CommandBus, CommandHandler, command_handler,
    Query, QueryBus, QueryHandler, query_handler,
    pyfly_application,
    enable_core_stack, register_core_stack,
)
```

### Web layer

```python
from pyfly.starters.web import (
    rest_controller, controller, controller_advice, exception_handler,
    request_mapping, get_mapping, post_mapping, put_mapping,
    patch_mapping, delete_mapping, sse_mapping,
    Body, PathVar, QueryParam, Header, Cookie, File, UploadedFile, Valid,
    enable_web_stack, register_web_stack,
)
```

### Domain layer

```python
from pyfly.starters.domain import (
    # DDD primitives
    Entity, ValueObject, AggregateRoot, DomainEvent, Specification,
    DomainRepository, DomainException, BusinessRuleViolation,
    AggregateNotFound,
    # Carried through from the core re-exports
    Command, CommandHandler, command_handler,
    Query, QueryHandler, query_handler,
    rest_controller, service, configuration,
    pyfly_application,
    enable_domain_stack, register_domain_stack,
)
```

## Cross-language correspondence

| Java | .NET | Python |
|------|------|--------|
| `fireflyframework-starter-core` | `services.AddFireflyCore(...)` | `@enable_core_stack` / `register_core_stack(app)` |
| (web bundled in core) | (web bundled in core) | `@enable_web_stack` / `register_web_stack(app)` *(new in v26.05.03)* |
| `fireflyframework-starter-application` | `services.AddFireflyApplication(...)` | `@enable_application_stack` / `register_application_stack(app)` |
| `fireflyframework-starter-data` | `services.AddFireflyData(...)` | `@enable_data_stack` / `register_data_stack(app)` |
| `fireflyframework-starter-domain` | `services.AddFireflyDomain(...)` | `@enable_domain_stack` / `register_domain_stack(app)` |

## Composing starters

Starters compose. A typical pattern: stack `@enable_application_stack`
*on top of* a vendor- or product-specific decorator that adds your own
beans or property defaults. Multiple `@enable_*_stack` decorators on
the same class union their property dicts, with later decorators
winning on key overlaps.

```python
@enable_web_stack          # add the web tier on top of the application bundle
@enable_application_stack  # base bundle: core + security + scheduling + …
@pyfly_application(name="acme-api", scan_packages=["acme"])
class Application:
    pass
```

## Property layering recap

```
┌─────────────────────────────────────────────────────────────┐
│  framework defaults  (pyfly-defaults.yaml — most modules    │
│                       disabled by default for safety)       │
├─────────────────────────────────────────────────────────────┤
│  starter defaults    (@enable_*_stack — turns the bundle on) │
├─────────────────────────────────────────────────────────────┤
│  user pyfly.yaml     (your project root + config/)          │
├─────────────────────────────────────────────────────────────┤
│  profile overlays    (pyfly-{profile}.yaml)                  │
├─────────────────────────────────────────────────────────────┤
│  environment vars    (PYFLY_X_Y at read time)                │
└─────────────────────────────────────────────────────────────┘
                                 ▲
                        each layer overrides the one above
```

`register_*_stack(app)` is the only exception — it stamps starter
values **on top** of everything (including the user yaml) because it's
called explicitly.
