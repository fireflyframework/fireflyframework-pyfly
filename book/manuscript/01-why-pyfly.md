<span class="eyebrow">Chapter 1</span>

# Why PyFly? {.chtitle}

::: figure art/openers/ch01.svg | &nbsp;

By the end of this chapter you will have installed PyFly, scaffolded the **Lumen** wallet service, and run it locally — with structured logging, a live health endpoint, and interactive API docs already working, all without a single line of boilerplate.

---

## The cohesion problem

Picture your first day on a new Python microservice. Before you write one line of business logic you spend the first two weeks making choices.

Which web framework do you reach for? FastAPI, Flask, Starlette, Django — each is reasonable and each introduces its own idioms. Which ORM? SQLAlchemy (sync or async?), Tortoise, Beanie? How do you wire dependencies — dependency-injector, python-inject, or a hand-rolled factory module? How do you handle configuration — pydantic-settings, python-dotenv, dynaconf? And how should the project be laid out? Every team invents its own answer.

You eventually assemble a bespoke stack, glue it together with duct tape and good intentions, and ship. Six months later a second team starts a new service — and makes entirely different choices. Now you have two codebases with different conventions, different testing strategies, different deployment patterns, and no shared understanding of how anything works.

**Python gives you infinite choice. What it does not give you is cohesion.**

::: figure art/figures/01-choice.svg | Figure 1.1 — Infinite choice, no cohesion.

The stack-assembly problem is not a skills failure — it is a tooling gap. Java developers solved it years ago with Spring Boot: one opinionated framework that makes sensible choices for you, lets you override what matters, and enforces a common idiom across every service. PyFly is that framework for Python.

---

## What is PyFly?

PyFly is a **cohesive, full-stack, async-native framework** for building production-grade Python applications — microservices, monoliths, and libraries alike. It makes the stack decisions for you: dependency injection, HTTP routing, database access, messaging, caching, security, observability — all integrated, all consistent, all with production-ready defaults from the very first `pyfly run`.

Under the hood PyFly delegates to the best async libraries in the Python ecosystem — Starlette for HTTP, SQLAlchemy (async) for relational data, structlog for logging, Pydantic for validation — but you never import them directly. You depend on **PyFly's ports** (Python `Protocol` classes), and the DI container wires the concrete adapters at startup. Swap PostgreSQL for MongoDB or Kafka for RabbitMQ without touching a single line of business logic.

PyFly is the **official Python implementation of the Firefly Framework**, a battle-tested enterprise platform originally built for Java (40+ modules in production). It brings the same programming model to Python 3.12+, not as a port but as a native implementation reimagined for `async/await` and type hints.

Its architecture rests on four layers — foundation, application, infrastructure, and integration — each composed of focused modules that interlock cleanly.

::: figure art/figures/01-layers.svg | Figure 1.2 — PyFly's four module layers.

Every layer respects the same **hexagonal architecture** principle: your code lives in the centre and depends only on ports. Adapters live at the edges and can be swapped without disturbing the core.

!!! spring "Spring parity"
    If you are coming from Spring Boot, PyFly will feel like home almost immediately. `@pyfly_application` is your `@SpringBootApplication`. `@service`, `@rest_controller`, and `@repository` are the exact stereotypes you know. Constructor-injection from type hints mirrors `@Autowired` with no XML or reflection magic. The `pyfly.yaml` configuration hierarchy (defaults → profile → env vars) maps directly to `application.yaml` + profiles. A **Spring parity** callout like this one appears throughout the book wherever the concepts align closely enough to save you the mental translation work.

---

## Installing PyFly

Lumen is the fintech wallet platform you will grow across the book: ledger entries, wallet balances, and eventually cross-service transfers. This section gets the skeleton running.

First, verify you have Python 3.12 or later:

::: listing terminal | Listing 1.1 — Install PyFly and scaffold Lumen
python --version
# Python 3.12.0 or later

# Install PyFly using the interactive installer
bash install.sh

# Verify the CLI is available
pyfly --version

# Scaffold the Lumen service
pyfly new lumen
cd lumen
:::

The `pyfly new` command generates a complete project structure with sensible defaults.

!!! tip "Tip"
    Run `pyfly new` **without arguments** to enter interactive mode. It walks you through archetype and feature selection with arrow-key navigation — handy when you want to pre-select extras like relational data or messaging support.

The generated layout is intentionally minimal:

```
lumen/
+-- pyproject.toml          # Python project metadata and dependencies
+-- pyfly.yaml              # PyFly configuration
+-- src/
|   +-- lumen/
|       +-- __init__.py
|       +-- app.py          # Application entry point
+-- tests/
    +-- __init__.py
```

As Lumen grows you will add `controllers.py`, `services.py`, `repositories.py`, and `models.py` alongside `app.py` — one file per architectural layer, matching Figure 1.2 exactly.

---

## Your first run

Open `src/lumen/app.py`. The scaffolder has already written the application entry point for you.

::: listing lumen/app.py | Listing 1.2 — The Lumen application entry point
from pyfly.core import pyfly_application, PyFlyApplication


@pyfly_application(
    name="lumen",
    version="0.1.0",
    scan_packages=["lumen"],
    description="Lumen — fintech wallet and ledger service",
)
class Application:
    pass
:::

`@pyfly_application` is PyFly's bootstrap decorator. It sets the service identity used in logs and health payloads, and — critically — tells the framework which packages to scan for beans via `scan_packages`. Every class decorated with `@service`, `@rest_controller`, `@repository`, or `@configuration` inside the `lumen` package will be auto-discovered and registered in the DI container when the application starts.

The `Application` class body is intentionally empty. You never instantiate it. `PyFlyApplication` does that during startup, executing this sequence: load configuration from `pyfly.yaml`, configure structured logging, print the startup banner, scan packages, initialize the `ApplicationContext`, and log startup timing.

Now start the server:

::: listing terminal | Listing 1.3 — Run it
pyfly run --reload
:::

The `--reload` flag watches source files and restarts the server automatically — ideal for development. After a moment you will see the PyFly ASCII banner followed by structured startup events:

```
  PyFly v26.06.x | Python 3.12.0

2026-01-15T10:30:00Z [info] starting_application  app=lumen version=0.1.0
2026-01-15T10:30:00Z [info] loaded_config          source=pyfly-defaults.yaml (framework defaults)
2026-01-15T10:30:00Z [info] loaded_config          source=pyfly.yaml
2026-01-15T10:30:00Z [info] scanned_package        package=lumen beans_found=0
2026-01-15T10:30:00Z [info] application_started    app=lumen startup_time_s=0.012 beans_initialized=0
INFO:     Uvicorn running on http://0.0.0.0:8080
```

!!! note "Note"
    Two endpoints are already live before you write a single handler. Open `http://localhost:8080/docs` in your browser to see the interactive Swagger UI (ReDoc is at `/redoc`, the raw OpenAPI spec at `/openapi.json`). If you enabled the actuator in `pyfly.yaml` (`actuator.enabled: true`), the health endpoint responds at `http://localhost:8080/actuator/health`. Both are provided by the framework automatically — you opt out of them, not into them.

---

## What you built {.recap}

In under five minutes you installed PyFly, scaffolded a production-shaped service, and ran it locally. Lumen already has structured JSON-capable logging with correlation IDs, interactive API documentation, and a framework-managed startup lifecycle — and the only code you touched was three lines of metadata in `app.py`.

That is the PyFly promise: decisions made for you, conventions consistent across every service, and production-ready defaults from the very first run.

---

## Try it yourself {.exercises}

1. **Rename the service.** Open `pyfly.yaml` and change the `name` field from `lumen` to `lumen-wallet`. Also update the `name` parameter in `@pyfly_application`. Re-run with `pyfly run --reload` and confirm the new name appears in the startup log.

2. **Explore the live docs.** With the server running, open `http://localhost:8080/docs`. Notice the service name and version at the top of the Swagger UI. Then navigate to `http://localhost:8080/redoc` and compare the two doc renderers. If you enable the actuator (`pyfly.yaml` → `actuator.enabled: true`), check the health response at `http://localhost:8080/actuator/health`.

3. **Map folders to layers.** Look at the generated project structure and match each directory or file to one of the four module layers in Figure 1.2. Which layer does `app.py` belong to? Where will `services.py` live when you create it in the next chapter? Where will `repositories.py` sit?
