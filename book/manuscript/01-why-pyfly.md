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

We will take this one short step at a time. Nothing here assumes prior PyFly experience — if you can run a command in a terminal, you can follow along.

!!! note "New term: uv"
    [uv](https://docs.astral.sh/uv/) is a fast Python package manager and project runner (think `pip` + `virtualenv` + a task runner, rolled into one binary). PyFly recommends it, and every command in this book that starts with `uv run` simply means "run this inside the project's virtual environment". If you have only ever used `pip`, you lose nothing — uv reads the same standard `pyproject.toml`.

**Step 1 — Check your prerequisites.** PyFly targets Python 3.12+ and uses uv. Confirm both are installed:

::: listing terminal | Listing 1.1 — Verify prerequisites
python --version
# Python 3.12.0 or later

uv --version
# uv 0.5.0 or later
:::

If either command is missing or reports an older version, install it before continuing (uv's install instructions are one short script on its website). Everything else PyFly needs, it pulls in for you.

**Step 2 — Scaffold the project.** A single command generates a complete, production-shaped project directory:

::: listing terminal | Listing 1.2 — Scaffold the Lumen wallet service
# web-api archetype, web feature
uv run pyfly new lumen --archetype web-api --features web
:::

!!! note "New term: scaffold / archetype"
    To *scaffold* is to generate a ready-made project skeleton so you start from working code instead of an empty folder. An *archetype* is the template that scaffolding uses — `web-api` is the REST-service template. The `--features web` flag adds the ASGI server dependency (the piece that actually accepts HTTP requests). `uv run pyfly new` calls PyFly's archetype registry and writes the whole directory in one shot.

**Step 3 — Move into the project and install dependencies.**

::: listing terminal | Listing 1.3 — Enter the project and sync dependencies
cd lumen

# Install dependencies (including the pyfly CLI and ASGI server)
uv sync
:::

`uv sync` reads the declared dependencies from `pyproject.toml` and installs them into a project-local virtual environment — including the `pyfly` command itself (available via the `cli` extra). The first sync downloads packages; subsequent syncs are near-instant because uv caches everything.

!!! tip "Run it: confirm the CLI works"
    From the project root, run `uv run pyfly --help`. You should see the PyFly command list (`new`, `run`, and friends). If that prints, your toolchain is healthy and you are ready to inspect the generated project.

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

!!! note "New term: ASGI"
    *ASGI* (Asynchronous Server Gateway Interface) is the standard contract between a Python web server and an async web application — the modern, async successor to WSGI. You do not have to implement it; PyFly hands the server a ready-made ASGI `app` object. Just remember: the *server* (Uvicorn or Granian) speaks ASGI to your *app*.

**Step 1 — Open the application declaration.** Look at `src/lumen/app.py`:

::: listing lumen/app.py | Listing 1.4 — Application declaration (@pyfly_application)
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

!!! note "New terms: DI and the ApplicationContext"
    *Dependency injection* (DI) means a class declares what it needs in its constructor and the framework supplies those collaborators, rather than the class building them itself. The *ApplicationContext* (often just "the container") is the registry that holds every wired object — PyFly calls each one a *bean*, the same term Spring uses. `scan_packages` is what populates it.

**Step 2 — Open the ASGI entry point.** Now look at `src/lumen/main.py`:

::: listing lumen/main.py | Listing 1.5 — ASGI entry point (main.py)
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
    _pyfly._host = str(_pyfly.config.get("pyfly.server.host", "0.0.0.0"))
    _pyfly._port = int(_pyfly.config.get("pyfly.server.port", 8080))
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

!!! note "What just happened"
    Two small files split one job in two. `app.py` answers *what is this application and where does its code live* (name, version, scan packages). `main.py` answers *how does a web server reach it* — it bootstraps PyFly into an `ApplicationContext`, then exposes a single ASGI `app` object. When you later add a controller or service, you edit neither file: you just drop the new class into one of the scanned packages and PyFly wires it on the next boot.

---

## Project files

The other two files you will edit most often are `pyproject.toml` and `pyfly.yaml`. Each plays a distinct role: `pyproject.toml` manages dependencies, while `pyfly.yaml` owns every runtime knob.

**Step 1 — Read the dependency manifest.** `pyproject.toml` records the project's dependencies. Notice the extras:

::: listing lumen/pyproject.toml | Listing 1.6 — pyproject.toml (key sections)
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

!!! note "New term: extras"
    An *extra* is an optional bundle of dependencies named in square brackets — `pyfly[web]` means "install PyFly plus its `web` optional group". Extras keep your install lean: you pull in a database driver, a CLI, or a faster server only when you actually use it.

The `web` extra in `pyfly[web]` bundles Uvicorn together with the ASGI adapter. The `cli` extra (add it when you need the `pyfly` command outside of `uv run`) provides the command-line tooling. Other extras — `data-relational`, `granian` — are opt-in; you add them only when a feature needs them. The Lumen wallet sample, for example, declares `pyfly[cli,web,data-relational]` because it connects to SQLite.

**Step 2 — Read the runtime configuration.** `pyfly.yaml` is where all runtime settings live:

::: listing lumen/pyfly.yaml | Listing 1.7 — pyfly.yaml (scaffold default)
pyfly:
  app:
    name: lumen-demo
    version: 0.1.0
    module: lumen_demo.main:app

  server:
    port: 8080
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

The `module` key tells `pyfly run` exactly where the ASGI `app` lives — `lumen_demo.main:app`. The application listens on `pyfly.server.port` (default `8080`); server type, actuator endpoints, and log level all have sensible defaults too, so you override only what you actually need to change.

!!! spring "Spring parity"
    `pyfly.server.port` is the direct counterpart of Spring Boot's `server.port`, right down to the `8080` default. The matching environment-variable override is `PYFLY_SERVER_PORT` (Spring's `SERVER_PORT`). Application identity follows the same pattern: `pyfly.app.name` and `pyfly.app.version` mirror `spring.application.name`.

!!! warning "Renamed key: use server.port, not web.port"
    Earlier PyFly releases used `pyfly.web.port` (and the `PYFLY_WEB_PORT` environment variable) for the listen port. Both were **removed** in v26.06.102 in favour of `pyfly.server.port` / `PYFLY_SERVER_PORT`. If you copy an old config and the port seems ignored, this is almost always why — rename the key under `server:` and the override takes effect.

!!! note "Default server: Granian vs Uvicorn"
    PyFly's default ASGI server is **Granian**, a high-performance Rust-based server. The scaffold's `pyfly[web]` dependency bundles **Uvicorn** instead (lighter install). The two are interchangeable: pass `--server uvicorn` on the command line, or add `pyfly[granian]` to your dependencies to use Granian. The Lumen wallet sample runs with `--server uvicorn` for this reason.

---

## Your first run

With the project in place, start the server.

**Step 1 — Start the server.** From the project root, run:

::: listing terminal | Listing 1.8 — Run the server with uv
uv run pyfly run --server uvicorn
:::

!!! note "Why `--server uvicorn`"
    PyFly's default server is Granian, but the `pyfly[web]` extra ships Uvicorn (a lighter install). Passing `--server uvicorn` tells PyFly to use the server you actually have. If you ever see an error about Granian not being installed, this flag is the fix — or add `pyfly[granian]` and drop the flag.

After a moment you will see the PyFly ASCII banner followed by a stream of structured startup events:

```
                _____.__
______ ___.__._/ ____\  | ___.__.
\____ <   |  |\   __\|  |<   |  |
|  |_> >___  | |  |  |  |_\___  |
|   __// ____| |__|  |____/ ____|
|__|   \/                 \/

:: PyFly Framework :: (v26.06.110) (Python 3.13.13)
Copyright 2026 Firefly Software Foundation. | Apache License 2.0
2026-06-07 20:34:32,442 [INFO] pyfly.core: starting_application |
    app=lumen-demo version=0.1.0 python=3.13.13 pid=72300
2026-06-07 20:34:32,442 [INFO] pyfly.core: runtime_environment |
    os=Darwin os_version=25.5.0 arch=arm64 cpus=11
2026-06-07 20:34:32,442 [INFO] pyfly.core: loaded_config |
    source=pyfly-defaults.yaml (framework defaults)
2026-06-07 20:34:32,442 [INFO] pyfly.core: loaded_config |
    source=pyfly.yaml
2026-06-07 20:34:32,442 [INFO] pyfly.core: scanned_package |
    package=lumen_demo.controllers beans_found=1
2026-06-07 20:34:32,580 [INFO] pyfly.core: bean_summary |
    total=127 services=6 repositories=2 controllers=4
2026-06-07 20:34:32,580 [INFO] pyfly.core: mapped_endpoints | count=5
2026-06-07 20:34:32,580 [INFO] pyfly.core: request_mapping |
    method=POST path=/api/v1/wallets handler=open_wallet
2026-06-07 20:34:32,580 [INFO] pyfly.core: api_documentation |
    swagger_ui=http://0.0.0.0:8080/docs
2026-06-07 20:34:32,580 [INFO] pyfly.core: management_server |
    url=http://0.0.0.0:9090 endpoints=actuator + admin
2026-06-07 20:34:32,580 [INFO] pyfly.core: admin_dashboard |
    url=http://0.0.0.0:9090/admin
2026-06-07 20:34:32,581 [INFO] pyfly.core: server_started |
    server=uvicorn host=0.0.0.0 port=8080 workers=1
```

Read these log lines as a story of what the framework did on your behalf. Each event has a name rather than a raw message so log aggregators can filter by key:

1. **`starting_application`** — PyFly announces itself with the service name and version you declared, giving every log aggregator a clean filter from the very first event.
2. **`runtime_environment`** — OS, architecture, and CPU count are emitted once on boot, making it easy to correlate behaviour with the environment later.
3. **`loaded_config`** (×2) — configuration is layered: the framework ships `pyfly-defaults.yaml` with safe production defaults; your `pyfly.yaml` overrides only what you need. Activate a profile (e.g., `--profile prod`) and you will see a third entry.
4. **`scanned_package`** — the DI container found one `@rest_controller` in the web controllers package. `beans_found` grows as you add services and repositories.
5. **`bean_summary`** — the total DI context size, including framework-internal beans. Even with 127 beans registered, PyFly boots in well under a second.
6. **`management_server`** — the actuator endpoints and admin dashboard are served on a *separate* management port (default `9090`), independent of the app's `8080`.
7. **`admin_dashboard`** — the exact URL of the live admin UI, `http://0.0.0.0:9090/admin`.
8. **`server_started`** — Uvicorn is accepting connections on the application port.

!!! note "What just happened"
    You ran one command and PyFly did the work of an entire boilerplate sprint: it loaded layered configuration, scanned your packages, wired the DI container, mapped your HTTP routes, published interactive API docs, brought up a separate management server with health checks and an admin dashboard, and started accepting requests — all logged as named, machine-filterable events. Leave this server running in its terminal; you will call it from a second terminal in the next steps. Press `Ctrl+C` when you want to stop it.

!!! spring "Spring parity"
    Serving actuator and the admin dashboard on a dedicated port mirrors Spring Boot's `management.server.port`. In PyFly the key is `pyfly.management.server.port` (default `9090`). Set it equal to `pyfly.server.port` for single-port behaviour, or set it to `-1` to disable the management endpoints entirely. By default this port is **open and unauthenticated** (also Spring parity) — fine for local development, but in production secure it with `pyfly.management.security.enabled: true`.

Two endpoints are already live before you have written a single line of application logic. Open a *second* terminal (leave the server running in the first) and try the health check.

**Step 2 — Check the live health endpoint.** It lives on the management port, `9090`:

::: listing terminal | Listing 1.9 — Health check (management port 9090)
curl -s localhost:9090/actuator/health
:::

!!! note "New term: actuator"
    The *actuator* is PyFly's built-in operations layer: a small set of HTTP endpoints that report on the running app — `/actuator/health`, `/actuator/info`, and (when you expose them) metrics, environment, and more. It is the same concept, and the same `/actuator` base path, as Spring Boot Actuator. By default only `health` and `info` are exposed over HTTP; widen that with `pyfly.management.endpoints.web.exposure.include`.

You should see a JSON document with a top-level `"status": "UP"` and a `components` map — one entry per health indicator the framework registered:

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

**Step 3 — Open a wallet.** Now call the first business endpoint. Unlike the actuator, this lives on the *application* port, `8080`:

::: listing terminal | Listing 1.10 — Open a wallet (application port 8080)
curl -s -X POST localhost:8080/api/v1/wallets \
  -H 'content-type: application/json' \
  -d '{"owner_id":"u-1","currency":"EUR"}'
:::

The response is the new wallet's identifier (your UUID will differ):

```json
{"wallet_id": "wlt-c5bbb2a7-dd49-4321-932e-e4c6bfa5cc2c"}
```

!!! note "What just happened"
    That single `curl` travelled through the whole stack the framework built for you: the JSON body was validated against the `OpenWalletRequest` model, the `WalletController` turned it into an `OpenWallet` command, the command bus dispatched it to its handler, a `Wallet` aggregate was created and persisted, and the new id came back as JSON. You will build each of those layers yourself in later chapters — for now, notice that it already works end to end.

**Step 4 — Explore the interactive docs.** Open `http://localhost:8080/docs` in your browser for the interactive Swagger UI; `http://localhost:8080/redoc` gives you ReDoc; the raw OpenAPI spec is at `http://localhost:8080/openapi.json`. The docs live on the application port alongside your API.

!!! note "Note"
    All three doc URLs are emitted in the boot log (`api_documentation` entries) so you never have to remember them. The PyFly Admin Dashboard — a live view of beans, endpoints, health indicators, and recent log events — opens at `http://localhost:9090/admin` on the management port (its exact URL is the `admin_dashboard` boot-log line).

**Step 5 — Run the generated tests.** The scaffold ships a working test suite so you can confirm the toolchain end to end. Install the dev tools, then run pytest:

::: listing terminal | Listing 1.11 — Run the test suite
uv sync --group dev
uv run --group dev pytest -q
:::

!!! note "Expected output"
    You should see a short run of dots (or `PASSED` lines) and a green summary like `3 passed in 0.42s` — the exact count depends on the archetype. Any green summary means PyFly, your virtual environment, and pytest are all wired correctly. (The Lumen wallet sample you study throughout the book ships a far larger suite, but it runs the same way: `uv run --group dev pytest -q`.)

That completes the loop: install, scaffold, run, call, and test — all before writing a line of your own logic.

---

## What you built {.recap}

In under five minutes you installed PyFly, scaffolded a production-shaped service, and ran it locally. The table below summarises what Lumen already delivers — entirely from the framework — before you have written a single line of application logic.

| Capability | How you got it |
|---|---|
| Structured JSON logging with runtime metadata | Framework default — emitted on every boot |
| Interactive API docs (Swagger UI + ReDoc) | Built-in; always on, opt out via `pyfly.yaml` |
| Health endpoint (`/actuator/health` on port 9090) | `actuator.endpoints.enabled: true` in scaffold |
| Profile-aware configuration layering | `pyfly.yaml` + `--profile` flag |
| Auto-discovery DI container | `scan_packages` in `@pyfly_application` |
| Startup timing, bean summary, endpoint map | Emitted as structured log events on every boot |
| Admin Dashboard (`/admin` on management port 9090) | Enabled in the scaffold by default |

That is the PyFly promise: sensible decisions made for you, conventions consistent across every service, and production-ready defaults from the very first run.

---

## Try it yourself {.exercises}

1. **Rename the service.** Open `pyfly.yaml` and change `app.name` from `lumen-demo` to `lumen-wallet`. Also update the `name` parameter in `@pyfly_application`. Re-run with `uv run pyfly run --server uvicorn` and confirm the new name appears in the `starting_application` log line.

2. **Explore the live docs.** With the server running, open `http://localhost:8080/docs`. Notice the service name and version at the top of the Swagger UI. Then navigate to `http://localhost:8080/redoc` and compare the two doc renderers. Check the health response at `http://localhost:9090/actuator/health` (remember: the actuator lives on the management port, not the application port) and note which health indicators are already reporting. Open `http://localhost:9090/admin` to see the same information in the live dashboard.

3. **Switch to Granian.** Add `pyfly[granian]` to the `dependencies` list in `pyproject.toml`, run `uv sync`, then start the server without the `--server uvicorn` flag. Compare the `server_started` log line — the `server` field should now read `granian`.

4. **Map files to responsibilities.** Look at the two entry-point files `app.py` and `main.py`. Write a one-sentence description of what each one is responsible for. Where does the DI container come from? Where does the ASGI `app` object live? Which one would you edit to change the scan packages?

5. **Collapse to a single port.** Add `pyfly.management.server.port: 8080` under the `server`-sibling `management:` section in `pyfly.yaml` (so it equals `pyfly.server.port`). Re-run the server and watch the boot log: the `management_server` line disappears and `http://localhost:8080/actuator/health` now responds on the application port. Then try `pyfly.management.server.port: -1`, re-run, and confirm the actuator and admin routes are gone entirely.
