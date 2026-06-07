<span class="eyebrow">Chapter 1</span>

# Why PyFly? {.chtitle}

::: figure art/openers/ch01.svg | &nbsp;

By the end of this chapter you will have installed PyFly, scaffolded the **Lumen** wallet service, and run it locally — with structured logging, a live health endpoint, and interactive API docs already working, all without a single line of boilerplate.

---

## The cohesion problem

Picture your first day on a new Python microservice. Before you write one line of business logic, you face two weeks of architectural choices.

Which web framework do you reach for? FastAPI, Flask, Starlette, Django — each is reasonable, each introduces its own idioms. Which ORM? SQLAlchemy (sync or async?), Tortoise, Beanie? How do you wire dependencies — dependency-injector, python-inject, or a hand-rolled factory module? How do you manage configuration — pydantic-settings, python-dotenv, dynaconf? And how should the project be laid out? Every team invents its own answer.

You eventually assemble a bespoke stack, glue it together with good intentions, and ship. Six months later a second team starts a new service — and makes entirely different choices. Now you have two codebases with incompatible conventions, different testing strategies, different deployment patterns, and no shared understanding of how anything works.

**Python gives you infinite choice. What it does not give you is cohesion.**

::: figure art/figures/01-choice.svg | Figure 1.1 — Infinite choice, no cohesion.

The stack-assembly problem is not a skills failure — it is a tooling gap. Java developers solved it years ago with Spring Boot: one opinionated framework that makes sensible choices for you, lets you override what matters, and enforces a consistent idiom across every service. PyFly brings that same discipline to Python.

---

## What is PyFly?

The solution to the cohesion problem is not to ban choice — it is to make one good set of choices and package them as a framework. That is precisely what PyFly does.

PyFly is a **cohesive, full-stack, async-native framework** for building production-grade Python applications — microservices, monoliths, and libraries alike. It makes the stack decisions for you: dependency injection, HTTP routing, database access, messaging, caching, security, observability — all integrated, all consistent, all with production-ready defaults from the very first `pyfly run`.

Under the hood, PyFly delegates to battle-tested async libraries in the Python ecosystem — Starlette for HTTP, SQLAlchemy (async) for relational data, structlog for logging, Pydantic for validation — but you never import them directly. You depend on **PyFly's ports** (Python `Protocol` classes), and the DI container wires the concrete adapters at startup. Swap PostgreSQL for MongoDB, or Kafka for RabbitMQ, without touching a single line of business logic.

PyFly is the **official Python implementation of the Firefly Framework**, a battle-tested enterprise platform originally built for Java (40+ modules in production). It brings the same programming model to Python 3.12+, not as a port but as a native reimplementation designed around `async/await` and type hints.

Its architecture rests on four layers — foundation, application, infrastructure, and integration — each composed of focused modules that interlock cleanly.

::: figure art/figures/01-layers.svg | Figure 1.2 — PyFly's four module layers.

Every layer follows the same **hexagonal architecture** principle: your code lives in the centre and depends only on ports; adapters live at the edges and can be swapped without disturbing the core. This design lets the framework grow with you — start with a simple REST service and graduate to CQRS, event-driven messaging, or saga orchestration without rewriting the foundation you built on day one.

!!! spring "Spring parity"
    If you are coming from Spring Boot, PyFly will feel like home almost immediately. `@pyfly_application` is your `@SpringBootApplication`. `@service`, `@rest_controller`, and `@repository` are the exact stereotypes you know. Constructor-injection from type hints mirrors `@Autowired` with no XML or reflection magic. The `pyfly.yaml` configuration hierarchy (defaults → profile → env vars) maps directly to `application.yaml` + profiles. A **Spring parity** callout like this one appears throughout the book wherever the concepts align closely enough to save you the mental translation work.

---

## Installing PyFly

To make these ideas concrete, you will build **Lumen** — a fintech wallet platform — throughout the book. Lumen starts simply: a REST API that records ledger entries and reports wallet balances. By the final chapter it will span multiple services communicating over events, with sagas coordinating cross-service transfers. Starting with a well-structured skeleton saves you the pain of retrofitting architecture later, so PyFly's scaffolder generates that structure for you up front.

First, verify you have Python 3.12 or later and [uv](https://docs.astral.sh/uv/) (PyFly's recommended package manager):

::: listing terminal | Listing 1.1 — Verify prerequisites and scaffold Lumen
python --version
# Python 3.12.0 or later

uv --version
# uv 0.5.0 or later

# Scaffold the Lumen wallet service (web-api archetype, web feature)
uv run pyfly new lumen --archetype web-api --features web

cd lumen

# Install dependencies (including the pyfly CLI and ASGI server)
uv sync
:::

`uv run pyfly new` calls the PyFly archetype registry and generates a production-shaped project directory in one shot. The `--archetype web-api` flag selects the REST service template; `--features web` adds the ASGI server dependency. `uv sync` installs the declared dependencies, including the `pyfly` command itself (available via the `cli` extra in `pyproject.toml`).

!!! tip "Tip"
    Run `pyfly new` **without arguments** to enter interactive mode. It walks you through archetype and feature selection with arrow-key navigation — handy when you want to pre-select extras like relational data or messaging support.

The scaffolder prints the generated layout as a tree:

```
╭──────────────── Created web-api project ─────────────────╮
│ lumen-demo/                                               │
│ ├── .env.example                                          │
│ ├── .gitignore                                            │
│ ├── Dockerfile                                            │
│ ├── README.md                                             │
│ ├── pyfly.yaml                                            │
│ ├── pyproject.toml                                        │
│ ├── src/lumen_demo/__init__.py                            │
│ ├── src/lumen_demo/app.py                                 │
│ ├── src/lumen_demo/controllers/__init__.py                │
│ ├── src/lumen_demo/controllers/health_controller.py       │
│ ├── src/lumen_demo/controllers/todo_controller.py         │
│ ├── src/lumen_demo/main.py                                │
│ ├── src/lumen_demo/models/__init__.py                     │
│ ├── src/lumen_demo/models/todo.py                         │
│ ├── src/lumen_demo/repositories/__init__.py               │
│ ├── src/lumen_demo/repositories/todo_repository.py        │
│ ├── src/lumen_demo/services/__init__.py                   │
│ ├── src/lumen_demo/services/todo_service.py               │
│ ├── tests/__init__.py                                     │
│ ├── tests/conftest.py                                     │
│ └── tests/test_todo_service.py                            │
╰───────────────────────────────────────────────────────────╯

  Next steps:
    cd lumen-demo
    uv sync --group dev
    pyfly run --reload
```

The apparent simplicity is deliberate. `pyproject.toml` gives you a standards-compliant project from day one — no `setup.py` legacy debt. `pyfly.yaml` is the single source of truth for every runtime setting, from log level to database URL, so configuration never scatters across a dozen files. The `src/` layout prevents the `lumen` package from being accidentally importable without an explicit install, catching import-path bugs early. The scaffolder also generates runnable sample controllers, services, and repositories so you have working code to study immediately rather than an empty shell.

---

## Two files that matter

Understanding the scaffold's entry-point structure early will save you confusion later. The scaffold generates two files that work together: `app.py` declares the application and `main.py` exposes the ASGI `app` that the server imports. Each has a distinct responsibility.

Open `src/lumen/app.py`:

::: listing lumen/app.py | Listing 1.2 — Application declaration (@pyfly_application)
from pyfly.core import pyfly_application


@pyfly_application(
    name="lumen-demo",
    scan_packages=[
        "lumen_demo.controllers",
        "lumen_demo.services",
        "lumen_demo.repositories",
    ],
)
class Application:
    pass
:::

Short as it is, every parameter pulls its weight:

- **`name`** — the service identity. It appears in every structured log event, in the OpenAPI title, and in health-check payloads — an unambiguous handle when you are reading aggregated logs from a dozen services.
- **`scan_packages`** — the instruction that drives the whole DI system. PyFly walks every listed module and registers any class decorated with `@service`, `@rest_controller`, `@repository`, or `@configuration` in the `ApplicationContext`. Add a new service class anywhere in those packages and it is wired automatically; no explicit registration code required.

The `Application` class body is intentionally empty. You never instantiate it yourself — `PyFlyApplication` does that during startup: it loads configuration from `pyfly.yaml`, configures structured logging, prints the startup banner, scans packages, initialises the `ApplicationContext`, and logs startup timing.

Now open `src/lumen/main.py`:

::: listing lumen/main.py | Listing 1.3 — ASGI entry point (main.py)
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from starlette.applications import Starlette

from pyfly.core import PyFlyApplication
from pyfly.web.adapters.starlette import create_app

from lumen_demo.app import Application

# Bootstrap: load config, scan packages, build DI context
_pyfly = PyFlyApplication(Application)


@asynccontextmanager
async def _lifespan(app: Starlette) -> AsyncIterator[None]:
    """Manage application startup and shutdown lifecycle."""
    _pyfly._route_metadata = getattr(
        app.state, "pyfly_route_metadata", []
    )
    _pyfly._docs_enabled = getattr(
        app.state, "pyfly_docs_enabled", False
    )
    _pyfly._host = str(_pyfly.config.get("pyfly.web.host", "0.0.0.0"))
    _pyfly._port = int(_pyfly.config.get("pyfly.web.port", 8080))
    await _pyfly.startup()
    yield
    await _pyfly.shutdown()


app = create_app(
    title="lumen-demo",
    version="0.1.0",
    context=_pyfly.context,
    lifespan=_lifespan,
)
:::

This is the file that `pyfly run` (and any ASGI server like Uvicorn) discovers. The module-level `app` object is a Starlette application: `create_app` mounts every `@rest_controller` found in the DI context and wires the lifespan hook so startup and shutdown happen cleanly. The `@pyfly_application` decorator alone does **not** create the ASGI `app` — `main.py` is the explicit bridge between the DI world (`app.py`) and the HTTP world.

---

## Project files

The other two files you will edit most often are `pyproject.toml` and `pyfly.yaml`. Each plays a distinct role: `pyproject.toml` manages dependencies, while `pyfly.yaml` owns every runtime knob.

`pyproject.toml` records the project's dependencies. Notice the extras:

::: listing lumen/pyproject.toml | Listing 1.4 — pyproject.toml (key sections)
[project]
name = "lumen-demo"
version = "0.1.0"
description = "lumen-demo — built with PyFly"
requires-python = ">=3.12"
dependencies = [
    "pyfly[web]",
]

[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-cov>=5.0",
    "ruff>=0.3",
    "mypy>=1.8",
]
:::

The `web` extra in `pyfly[web]` bundles Uvicorn together with the ASGI adapter. The `cli` extra (add it when you need the `pyfly` command outside of `uv run`) provides the command-line tooling. Other extras — `data-relational`, `granian` — are opt-in; you add them only when a feature needs them. The Lumen wallet sample, for example, declares `pyfly[cli,web,data-relational]` because it connects to SQLite.

`pyfly.yaml` is where all runtime settings live:

::: listing lumen/pyfly.yaml | Listing 1.5 — pyfly.yaml (scaffold default)
pyfly:
  app:
    name: lumen-demo
    module: lumen_demo.main:app

  web:
    port: 8080
    adapter: auto

  server:
    type: "auto"
    event-loop: "auto"
    workers: 0

  actuator:
    endpoints:
      enabled: true

  admin:
    enabled: true

  logging:
    level:
      root: INFO
:::

The `module` key tells `pyfly run` exactly where the ASGI `app` lives — `lumen_demo.main:app`. Port, server type, actuator endpoints, and log level all have sensible defaults; you override only what you actually need to change.

!!! note "Default server: Granian vs Uvicorn"
    PyFly's default ASGI server is **Granian**, a high-performance Rust-based server. The scaffold's `pyfly[web]` dependency bundles **Uvicorn** instead (lighter install). The two are interchangeable: pass `--server uvicorn` on the command line, or add `pyfly[granian]` to your dependencies to use Granian. The Lumen wallet sample runs with `--server uvicorn` for this reason.

---

## Your first run

With the project in place, start the server. From the project root:

::: listing terminal | Listing 1.6 — Run the server with uv
uv run pyfly run --server uvicorn
:::

After a moment you will see the PyFly ASCII banner followed by a stream of structured startup events:

```
                _____.__
______ ___.__._/ ____\  | ___.__.
\____ <   |  |\   __\|  |<   |  |
|  |_> >___  | |  |  |  |_\___  |
|   __// ____| |__|  |____/ ____|
|__|   \/                 \/

:: PyFly Framework :: (v26.06.60) (Python 3.13.13)
Copyright 2026 Firefly Software Foundation. | Apache License 2.0
2026-06-07 20:34:32,442 [INFO] pyfly.core: starting_application |
    app=lumen version=1.0.0 python=3.13.13 pid=72300
2026-06-07 20:34:32,442 [INFO] pyfly.core: runtime_environment |
    os=Darwin os_version=25.5.0 arch=arm64 cpus=11
2026-06-07 20:34:32,442 [INFO] pyfly.core: loaded_config |
    source=pyfly-defaults.yaml (framework defaults)
2026-06-07 20:34:32,442 [INFO] pyfly.core: loaded_config |
    source=pyfly.yaml
2026-06-07 20:34:32,442 [INFO] pyfly.core: scanned_package |
    package=lumen.web.controllers beans_found=1
2026-06-07 20:34:32,580 [INFO] pyfly.core: bean_summary |
    total=127 services=6 repositories=2 controllers=4
2026-06-07 20:34:32,580 [INFO] pyfly.core: mapped_endpoints | count=5
2026-06-07 20:34:32,580 [INFO] pyfly.core: request_mapping |
    method=POST path=/api/v1/wallets handler=open_wallet
2026-06-07 20:34:32,580 [INFO] pyfly.core: api_documentation |
    swagger_ui=http://0.0.0.0:8080/docs
2026-06-07 20:34:32,581 [INFO] pyfly.core: server_started |
    server=uvicorn host=0.0.0.0 port=8080 workers=1
```

Read these log lines as a story of what the framework did on your behalf. Each event has a name rather than a raw message so log aggregators can filter by key:

1. **`starting_application`** — PyFly announces itself with the service name and version you declared, giving every log aggregator a clean filter from the very first event.
2. **`runtime_environment`** — OS, architecture, and CPU count are emitted once on boot, making it easy to correlate behaviour with the environment later.
3. **`loaded_config`** (×2) — configuration is layered: the framework ships `pyfly-defaults.yaml` with safe production defaults; your `pyfly.yaml` overrides only what you need. Activate a profile (e.g., `--profile prod`) and you will see a third entry.
4. **`scanned_package`** — the DI container found one `@rest_controller` in the web controllers package. `beans_found` grows as you add services and repositories.
5. **`bean_summary`** — the total DI context size, including framework-internal beans. Even with 127 beans registered, PyFly boots in well under a second.
6. **`server_started`** — Uvicorn is accepting connections.

Two endpoints are already live before you have written a single line of application logic. Try the health check first:

::: listing terminal | Listing 1.7 — Health check
curl -s localhost:8080/actuator/health
:::

```json
{
  "status": "UP",
  "components": {
    "cache_health_indicator": {
      "status": "UP",
      "details": {"adapter": "InMemoryCache", "latencyMs": 0.01}
    },
    "cqrs_health_indicator": {
      "status": "UP",
      "details": {"command_handlers": 3, "query_handlers": 2}
    },
    "eda_health": {
      "status": "UP",
      "details": {"adapter": "InMemoryEventBus"}
    },
    "db_health_indicator": {
      "status": "UP",
      "details": {"database": "sqlite"}
    }
  }
}
```

Then call the first business endpoint — opening a wallet:

::: listing terminal | Listing 1.8 — Open a wallet
curl -s -X POST localhost:8080/api/v1/wallets \
  -H 'content-type: application/json' \
  -d '{"owner_id":"u-1","currency":"EUR"}'
:::

```json
{"wallet_id": "wlt-c5bbb2a7-dd49-4321-932e-e4c6bfa5cc2c"}
```

Open `http://localhost:8080/docs` in your browser for the interactive Swagger UI; `http://localhost:8080/redoc` gives you ReDoc; the raw OpenAPI spec is at `/openapi.json`.

!!! note "Note"
    All three doc URLs are emitted in the boot log (`api_documentation` entries) so you never have to remember them. `http://localhost:8080/admin` opens the PyFly Admin Dashboard — a live view of beans, endpoints, health indicators, and recent log events.

---

## What you built {.recap}

In under five minutes you installed PyFly, scaffolded a production-shaped service, and ran it locally. The table below summarises what Lumen already delivers — entirely from the framework — before you have written a single line of application logic.

| Capability | How you got it |
|---|---|
| Structured JSON logging with runtime metadata | Framework default — emitted on every boot |
| Interactive API docs (Swagger UI + ReDoc) | Built-in; always on, opt out via `pyfly.yaml` |
| Health endpoint (`/actuator/health`) | `actuator.endpoints.enabled: true` in scaffold |
| Profile-aware configuration layering | `pyfly.yaml` + `--profile` flag |
| Auto-discovery DI container | `scan_packages` in `@pyfly_application` |
| Startup timing, bean summary, endpoint map | Emitted as structured log events on every boot |
| Admin Dashboard (`/admin`) | Enabled in the scaffold by default |

That is the PyFly promise: sensible decisions made for you, conventions consistent across every service, and production-ready defaults from the very first run.

---

## Try it yourself {.exercises}

1. **Rename the service.** Open `pyfly.yaml` and change `app.name` from `lumen-demo` to `lumen-wallet`. Also update the `name` parameter in `@pyfly_application`. Re-run with `uv run pyfly run --server uvicorn` and confirm the new name appears in the `starting_application` log line.

2. **Explore the live docs.** With the server running, open `http://localhost:8080/docs`. Notice the service name and version at the top of the Swagger UI. Then navigate to `http://localhost:8080/redoc` and compare the two doc renderers. Check the health response at `http://localhost:8080/actuator/health` and note which health indicators are already reporting.

3. **Switch to Granian.** Add `pyfly[granian]` to the `dependencies` list in `pyproject.toml`, run `uv sync`, then start the server without the `--server uvicorn` flag. Compare the `server_started` log line — the `server` field should now read `granian`.

4. **Map files to responsibilities.** Look at the two entry-point files `app.py` and `main.py`. Write a one-sentence description of what each one is responsible for. Where does the DI container come from? Where does the ASGI `app` object live? Which one would you edit to change the scan packages?
