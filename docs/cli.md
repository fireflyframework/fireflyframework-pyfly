# CLI Reference

The PyFly CLI provides command-line tools for project scaffolding, application management, database migrations, environment diagnostics, and framework information.

**Install:** `uv sync --extra cli` (requires Click, Rich, Jinja2, questionary)

**Entry point:** `pyfly` (registered as a console script via `pyproject.toml`)

---

## Table of Contents

- [Overview](#overview)
- [Global Options](#global-options)
- [pyfly new](#pyfly-new)
- [pyfly generate](#pyfly-generate-alias-g)
- [pyfly run](#pyfly-run)
- [pyfly info](#pyfly-info)
- [pyfly doctor](#pyfly-doctor)
- [pyfly db](#pyfly-db)
  - [pyfly db init](#pyfly-db-init)
  - [pyfly db migrate](#pyfly-db-migrate)
  - [pyfly db upgrade](#pyfly-db-upgrade)
  - [pyfly db downgrade](#pyfly-db-downgrade)
  - [Running Migrations on Startup](#running-migrations-on-startup)
- [pyfly license](#pyfly-license)
- [pyfly sbom](#pyfly-sbom)
- [Development Workflow](#typical-development-workflow)

---

## Overview

The CLI is built with [Click](https://click.palletsprojects.com/) for command parsing and [Rich](https://rich.readthedocs.io/) for beautiful terminal output. When you run `pyfly --help`, you'll see an ASCII banner followed by the command listing.

All commands are organized as a Click group under the `pyfly` entry point:

```
pyfly
├── new       — Create a new project
├── generate  — Scaffold individual artifacts (alias: g)
│   ├── controller    — REST or web/SSR controller + test
│   ├── service       — @service class + test
│   ├── repository    — Repository (SQL / Mongo / in-memory)
│   ├── entity        — Model/entity (Pydantic + ORM)
│   ├── dto           — Request/response DTOs
│   ├── aggregate     — DDD aggregate root with domain events
│   ├── command       — CQRS command + handler
│   ├── query         — CQRS query + handler
│   ├── event         — Domain event + listener
│   ├── saga          — Saga orchestration skeleton
│   ├── scheduled     — Scheduled job
│   ├── shell-command — @shell_component command
│   ├── migration     — Database migration (delegates to pyfly db migrate)
│   └── resource      — Full CRUD stack (entity + dto + repo + service + controller + test)
├── run       — Start the application server
├── info      — Display framework information
├── doctor    — Diagnose environment
├── db        — Database migration commands
│   ├── init      — Initialize Alembic
│   ├── migrate   — Generate migration
│   ├── upgrade   — Apply migrations
│   └── downgrade — Revert migrations
├── license   — Display the Apache 2.0 license
└── sbom      — Software Bill of Materials
```

### Architecture

The CLI module follows the same patterns as the rest of PyFly:

```
src/pyfly/cli/
├── __init__.py
├── main.py          # PyFlyCLI group, command registration
├── console.py       # Shared Rich console, theme, print_banner()
├── new.py           # pyfly new — project scaffolding (questionary TUI + Click CLI)
├── run.py           # pyfly run — application server
├── info.py          # pyfly info — environment information
├── doctor.py        # pyfly doctor — environment diagnostics
├── db.py            # pyfly db — Alembic migration management
├── license.py       # pyfly license — display Apache 2.0 license
├── sbom.py          # pyfly sbom — Software Bill of Materials
├── templates.py     # Jinja2-based template renderer
└── templates/       # Jinja2 template files (.j2)
    ├── pyproject.toml.j2
    ├── app.py.j2
    ├── pyfly.yaml.j2
    ├── dockerfile.j2
    ├── readme.md.j2
    ├── ...
    ├── hex/         # Hexagonal archetype templates
    ├── web/         # Web (SSR) archetype templates
    └── cli/         # CLI archetype templates
```

### Rich Console Theme

The CLI uses a custom Rich theme for consistent, colored output across all commands:

| Style | Color | Usage |
|-------|-------|-------|
| `info` | Cyan | Informational messages, labels |
| `success` | Bold green | Success indicators (checkmarks) |
| `warning` | Bold yellow | Warnings (non-fatal issues) |
| `error` | Bold red | Errors (fatal issues) |
| `pyfly` | Bold magenta | PyFly branding elements |
| `dim` | Dim | Secondary text, descriptions |

All commands share a single `console` instance from `pyfly.cli.console`, ensuring consistent styling. The `print_banner()` function renders the PyFly ASCII art banner.

---

## Global Options

```bash
pyfly --version    # Show PyFly version (from pyproject.toml)
pyfly --help       # Show help with ASCII banner and all commands
```

The `--version` flag reads the version from the `pyfly` package metadata.

---

## pyfly new

Create a new PyFly project with a complete directory structure, configuration files, and starter code. Supports six archetypes, selective feature inclusion, and an interactive mode.

### Usage

```bash
pyfly new <name> [OPTIONS]    # Direct mode
pyfly new [OPTIONS]           # Interactive mode (prompts for all options)
```

### Arguments

| Argument | Required | Description |
|----------|----------|-------------|
| `name` | No | Project name. Omit to enter interactive mode. |

The project name is converted to a valid Python package name: `my-service` becomes `my_service` for the package directory. In interactive mode the wizard also prompts for a **Package name** (pre-filled with that derived value); if you change it, the custom package name is honored and used for the `src/<package>/` layout and all template imports.

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--archetype` | `core` | Project archetype (see below) |
| `--features` | Per archetype | Comma-separated PyFly extras (e.g. `web,data-relational,cache`) |
| `--directory` | `.` | Parent directory where the project folder will be created |

### Archetypes

| Archetype | Description | Default Features |
|-----------|-------------|-----------------|
| `core` | Minimal microservice | *(none)* |
| `web-api` | Full REST API with layered architecture | `web` |
| `fastapi-api` | Full REST API with FastAPI and native OpenAPI | `fastapi` |
| `web` | Server-rendered web application with HTML templates | `web` |
| `hexagonal` | Hexagonal architecture (ports & adapters) | `web` |
| `library` | Reusable library package | *(none)* |
| `cli` | Command-line application with interactive shell | `shell` |

### Available Features

Features control which PyFly extras are included as dependencies and which config sections are generated:

| Feature | What it adds |
|---------|-------------|
| `web` | HTTP server (Starlette), REST controllers, OpenAPI docs |
| `fastapi` | HTTP server (FastAPI), REST controllers, native OpenAPI |
| `granian` | Granian ASGI server (Rust/tokio, highest throughput) |
| `hypercorn` | Hypercorn ASGI server (HTTP/2 and HTTP/3 support) |
| `data-relational` | Data Relational -- SQL databases (SQLAlchemy ORM) |
| `data-document` | Data Document -- Document databases (Beanie ODM) |
| `eda` | Event-driven architecture, in-memory event bus |
| `cache` | Caching with in-memory adapter |
| `client` | Resilient HTTP client with retry and circuit breaker |
| `security` | JWT authentication, password hashing |
| `scheduling` | Cron-based task scheduling |
| `observability` | Prometheus metrics, OpenTelemetry tracing |
| `cqrs` | Command/Query Responsibility Segregation |
| `shell` | Spring Shell-inspired CLI commands with DI |

### Core Archetype

The `core` archetype creates a minimal microservice with configuration and Docker support:

```
my-service/
├── pyproject.toml
├── pyfly.yaml
├── Dockerfile
├── README.md
├── .gitignore
├── .env.example
├── src/
│   └── my_service/
│       ├── __init__.py
│       ├── app.py
│       └── main.py
└── tests/
    ├── __init__.py
    └── conftest.py
```

### Web API Archetype

The `web-api` archetype creates a full REST API with layered controllers, services, models, and repositories — all using PyFly stereotypes. The example uses a Todo CRUD API with `title`, `completed`, and `description` fields:

```
my-api/
├── pyproject.toml
├── pyfly.yaml
├── Dockerfile
├── README.md
├── .gitignore
├── .env.example
├── src/
│   └── my_api/
│       ├── __init__.py
│       ├── app.py
│       ├── main.py
│       ├── controllers/
│       │   ├── __init__.py
│       │   ├── health_controller.py    # @rest_controller — /health
│       │   └── todo_controller.py      # @rest_controller — CRUD /todos
│       ├── services/
│       │   ├── __init__.py
│       │   └── todo_service.py         # @service — business logic
│       ├── models/
│       │   ├── __init__.py
│       │   └── todo.py                 # Pydantic request/response DTOs
│       └── repositories/
│           ├── __init__.py
│           └── todo_repository.py      # @repository — in-memory store
└── tests/
    ├── __init__.py
    ├── conftest.py
    └── test_todo_service.py
```

### Web Archetype

The `web` archetype creates a server-rendered HTML application with Jinja2 templates, static assets, and the `@controller` stereotype:

```
my-site/
├── pyproject.toml
├── pyfly.yaml
├── Dockerfile
├── README.md
├── .gitignore
├── .env.example
├── src/
│   └── my_site/
│       ├── __init__.py
│       ├── app.py
│       ├── main.py                     # ASGI entry with StaticFiles mount
│       ├── controllers/
│       │   ├── __init__.py
│       │   ├── health_controller.py    # @rest_controller — /health
│       │   └── home_controller.py      # @controller — / and /about
│       ├── services/
│       │   ├── __init__.py
│       │   └── page_service.py         # @service — page context data
│       ├── templates/
│       │   ├── base.html               # Base layout with nav and footer
│       │   ├── home.html               # Home page
│       │   └── about.html              # About page
│       └── static/
│           └── css/
│               └── style.css           # Minimal stylesheet
└── tests/
    ├── __init__.py
    ├── conftest.py
    └── test_home_controller.py
```

The `web` archetype uses `@controller` (instead of `@rest_controller`) for endpoints that return `TemplateResponse` objects. The `main.py` mounts a `/static` route for serving CSS, JavaScript, and images via Starlette's `StaticFiles`.

### Hexagonal Archetype

The `hexagonal` archetype creates a ports-and-adapters project with explicit domain, application, infrastructure, and API layers:

```
my-hex/
├── pyproject.toml
├── pyfly.yaml
├── Dockerfile
├── README.md
├── .gitignore
├── .env.example
├── src/
│   └── my_hex/
│       ├── __init__.py
│       ├── app.py
│       ├── main.py
│       ├── domain/
│       │   ├── __init__.py
│       │   ├── models.py              # Domain entities (dataclasses)
│       │   ├── events.py              # Domain events
│       │   └── ports/
│       │       ├── __init__.py
│       │       ├── inbound.py         # Use-case Protocols
│       │       └── outbound.py        # Repository Protocols
│       ├── application/
│       │   ├── __init__.py
│       │   └── services.py            # @service — implements inbound ports
│       ├── infrastructure/
│       │   ├── __init__.py
│       │   ├── config.py              # @configuration beans
│       │   └── adapters/
│       │       ├── __init__.py
│       │       └── persistence.py     # @repository — implements outbound ports
│       └── api/
│           ├── __init__.py
│           ├── controllers.py         # @rest_controller
│           └── dto.py                 # Pydantic request/response DTOs
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── domain/
    │   ├── __init__.py
    │   └── test_models.py
    └── application/
        ├── __init__.py
        └── test_services.py
```

### Library Archetype

The `library` archetype creates a minimal reusable library with PEP 561 `py.typed` marker:

```
my-library/
├── pyproject.toml
├── README.md
├── .gitignore
├── src/
│   └── my_library/
│       ├── __init__.py
│       └── py.typed
└── tests/
    ├── __init__.py
    └── conftest.py
```

### CLI Archetype

The `cli` archetype creates a command-line application with interactive shell, DI-powered commands, and service layer:

```
my-tool/
├── pyproject.toml
├── pyfly.yaml
├── Dockerfile
├── README.md
├── .gitignore
├── .env.example
├── src/
│   └── my_tool/
│       ├── __init__.py
│       ├── app.py
│       ├── main.py                    # CLI entry point (asyncio.run)
│       ├── commands/
│       │   ├── __init__.py
│       │   └── hello_command.py       # @shell_component — example commands
│       └── services/
│           ├── __init__.py
│           └── greeting_service.py    # @service — business logic
└── tests/
    ├── __init__.py
    ├── conftest.py
    └── test_hello_command.py
```

The CLI archetype differs from other archetypes in several ways:
- **No ASGI entry point** — uses `asyncio.run(pyfly.run())` instead of uvicorn/Starlette
- **No web section** in `pyfly.yaml` — no port, no adapter config
- **No `module:` field** in config — no ASGI app to reference
- **Shell enabled** — `pyfly.shell.enabled: true` is set automatically
- **Dockerfile** uses `CMD ["python", "-m", "my_tool.main"]` instead of uvicorn, no `EXPOSE`

### Interactive Mode

When `pyfly new` is run without a `NAME` argument, it enters interactive mode with a full-featured TUI experience powered by [questionary](https://questionary.readthedocs.io/):

```
$ pyfly new

  ╭─────────────────────────────────╮
  │   PyFly Project Generator       │
  ╰─────────────────────────────────╯

  ? Project name: my-service
  ? Package name: my_service
  ? Select archetype: (use arrow keys)
    ❯ core          Minimal microservice with DI container and config
      web-api       Full REST API with controller/service/repository layers
      web           Server-rendered HTML with Jinja2 templates and static assets
      hexagonal     Clean architecture with domain isolation
      library       Reusable library with py.typed and packaging best practices
      cli           Command-line application with interactive shell and DI

  ? Select features: (space to toggle, enter to confirm)
    ❯ [x] web          HTTP server, REST controllers, OpenAPI docs
      [ ] data-relational  Data Relational — SQL databases (SQLAlchemy ORM)
      [ ] data-document    Data Document — Document databases (Beanie ODM)
      [ ] cache        Caching with in-memory adapter
      [ ] security     JWT authentication, password hashing
      [ ] eda          Event-driven architecture, in-memory event bus
      [ ] client       Resilient HTTP client with retry and circuit breaker
      [ ] scheduling   Cron-based task scheduling
      [ ] observability  Prometheus metrics, OpenTelemetry tracing
      [ ] cqrs         Command/Query Responsibility Segregation
      [ ] shell        Spring Shell-inspired CLI commands with DI

  ╭─ Project Summary ───────────────╮
  │   Name:      my-service         │
  │   Package:   my_service         │
  │   Archetype: web-api            │
  │   Features:  web, data-relational │
  ╰─────────────────────────────────╯

  ? Create this project? Yes
```

**Features:**
- Arrow-key navigation for archetype selection (single-select)
- Space-bar toggling for feature selection (multi-select with checkbox)
- A dedicated **Package name** prompt — defaults to the package name derived from
  the project name, but any custom value you enter is passed through to
  `generate_project(..., package_name=...)` and used as the actual package
  directory and import root
- Confirmation summary with Rich-styled panel before creation
- Graceful handling of Ctrl+C (exits cleanly without traceback)

The library archetype skips the feature selection step since libraries don't include PyFly extras.

### Error Handling

If the target directory already exists, the command exits with an error. If an unknown feature is specified, the command lists valid features and exits.

### Examples

```bash
# Create a microservice (core archetype, no features)
pyfly new order-service

# Create a REST API (includes health controller, Todo CRUD example)
pyfly new order-api --archetype web-api

# Create a server-rendered web application with HTML templates
pyfly new my-site --archetype web

# Create a hexagonal project with data and cache
pyfly new order-svc --archetype hexagonal --features web,data-relational,cache

# Create a CLI tool with interactive shell
pyfly new admin-tool --archetype cli

# Create a shared library
pyfly new common-utils --archetype library

# Create in a specific directory
pyfly new payment-service --directory /projects

# Interactive mode
pyfly new
```

After creation, the CLI displays a Rich tree panel showing all created files and a hint to navigate into the project.

---

## pyfly generate (alias `g`)

Scaffold individual artifacts into the current project. The generator detects the
project package and archetype automatically (from `pyfly.yaml` / `src/` layout).

| Subcommand | Creates |
|---|---|
| `generate controller <Name>` | A REST controller (or web/SSR controller for the `web` archetype) + test |
| `generate service <Name>` | A `@service` class + test |
| `generate repository <Name>` | A repository (SQLAlchemy / Mongo / in-memory, data-aware) |
| `generate entity <Name>` | A model/entity (Pydantic + ORM when data is enabled) |
| `generate dto <Name>` | Request/response DTOs |
| `generate aggregate <Name>` | A DDD aggregate root with domain events |
| `generate command <Name>` | A CQRS command + handler |
| `generate query <Name>` | A CQRS query + handler |
| `generate event <Name>` | A domain event + listener |
| `generate saga <Name>` | A saga orchestration skeleton |
| `generate scheduled <Name>` | A scheduled job |
| `generate shell-command <Name>` | A `@shell_component` command |
| `generate migration` | A database migration (delegates to `pyfly db migrate`) |
| `generate resource <Name>` | Full CRUD stack: entity + dto + repository + service + controller + test |

Common flags: `--dry-run` (show planned files without writing), `--force` (overwrite existing files).

### Examples

```bash
pyfly g service Pricing
pyfly g resource Product
pyfly g command OpenWallet --dry-run
```

---

## pyfly run

Start the PyFly application using the auto-configured ASGI server (Granian, Uvicorn, or Hypercorn).

### Usage

```bash
pyfly run [OPTIONS]
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--host` | `0.0.0.0` | Bind address |
| `--port` | From `pyfly.yaml` or `8080` | Port number (resolved from: CLI flag → `pyfly.web.port` in config → `8080`) |
| `--server` | From config or `auto` | ASGI server: `granian`, `uvicorn`, `hypercorn` (auto-selects highest-priority installed) |
| `--workers` | From config or `0` | Number of worker processes (`0` = `cpu_count`) |
| `--reload` | `false` | Enable auto-reload on code changes (for development) |
| `--app` | Auto-discovered | Application import path (e.g., `myapp.main:app`) |

### Path Setup

Before discovery, `pyfly run` automatically adds the `src/` directory to `sys.path` if it exists. This allows running from a src-layout project root without installing the project first.

### Application Discovery

When `--app` is not provided, `pyfly run` attempts to discover the application automatically using a three-stage resolution:

1. **Canonical**: reads `pyfly.app.module` from `pyfly.yaml` (nested under `pyfly:` key)
2. **Flat fallback**: reads `app.module` from `pyfly.yaml` (flat layout)
3. **Auto-discovery**: scans `src/<package>/main.py` and constructs the import path

```yaml
# pyfly.yaml — canonical layout
pyfly:
  app:
    module: my_service.main:app
```

If neither `--app` nor a discoverable config file is found, the command exits with a clear error:

```
No application found.
Provide --app flag or create a pyfly.yaml in the current directory.
```

### Server Selection

When `--server` is not specified, `pyfly run` auto-selects the highest-priority installed ASGI server:

| Priority | Server | Condition |
|----------|--------|-----------|
| 1 | Granian | `granian` is importable |
| 2 | Uvicorn | `uvicorn` is importable |
| 3 | Hypercorn | `hypercorn` is importable |

### Requirements

Requires at least one ASGI server to be installed (Granian, Uvicorn, or Hypercorn). If none is available, the command suggests installing the `web` extra:

```
✗ No ASGI server found.
  Install the web extra: pyfly[web]
```

### Examples

```bash
# Development with auto-reload
pyfly run --reload

# Force Granian with 4 workers
pyfly run --server granian --workers 4

# Custom port
pyfly run --port 3000

# Explicit app path
pyfly run --app my_service.app:Application

# Production binding with Granian on all cores
pyfly run --host 0.0.0.0 --port 80 --server granian --workers 0
```

---

## pyfly info

Display comprehensive information about the PyFly installation and environment.

### Usage

```bash
pyfly info
```

### Output

The command displays two Rich tables:

**1. Environment Table**

| Field | Example |
|-------|---------|
| Python | 3.12.5 |
| Platform | macOS-14.5-arm64 |
| Architecture | arm64 |

**2. Installed Extras Table**

Shows the installation status of each optional module by attempting to import the underlying library. The table has two columns — **Extra** and **Status**:

| Extra | Detection Module |
|-------|-----------------|
| `web` | `starlette` |
| `data-relational` | `sqlalchemy` |
| `data-document` | `beanie` |
| `eda` | `aiokafka` |
| `kafka` | `aiokafka` |
| `rabbitmq` | `aio_pika` |
| `redis` | `redis` |
| `cache` | `redis` |
| `client` | `httpx` |
| `observability` | `prometheus_client` |
| `security` | `jwt` |
| `scheduling` | `croniter` |
| `cli` | `rich` |

Each extra shows as either `installed` (green) or `not installed` (dimmed).

This is useful for verifying which framework modules are available after installation, especially when using selective extras rather than `full`.

---

## pyfly doctor

Run a comprehensive health check on your development environment, verifying Python version, tools, and PyFly installation.

### Usage

```bash
pyfly doctor
```

### Checks Performed

**1. Python Version**

Verifies Python >= 3.12. Displays the exact version with a pass/fail indicator.

```
✓ Python 3.12.5          # Pass
✗ Python 3.11.2 (requires >=3.12)  # Fail
```

**2. Virtual Environment**

Checks if a virtual environment is active by comparing `sys.prefix` to `sys.base_prefix`. Shows a warning if not in a venv — this is non-fatal but recommended.

```
✓ Virtual environment active    # In venv
! No virtual environment detected  # Warning
```

**3. Required Tools**

Checks that essential tools are available on PATH using `shutil.which()`:

| Tool | Purpose | Impact if Missing |
|------|---------|-------------------|
| `git` | Version control | Overall check fails |
| `uv` | Package manager | Overall check fails |

**4. Optional Tools**

Checks for recommended development tools:

| Tool | Purpose | Impact if Missing |
|------|---------|-------------------|
| `uvicorn` | ASGI server (`pyfly run`) | Shown as `-` (dimmed) |
| `alembic` | Database migrations (`pyfly db`) | Shown as `-` (dimmed) |
| `ruff` | Linter & formatter | Shown as `-` (dimmed) |
| `mypy` | Type checker | Shown as `-` (dimmed) |

Missing optional tools are shown with a `-` dash indicator (dimmed), while missing required tools use `✗` (red) and cause the overall check to fail.

**5. PyFly Installation**

Verifies that PyFly itself is importable and displays the installed version:

```
✓ pyfly v26.05.01
```

### Summary

The doctor command ends with a clear summary:
- **All checks passed!** (green) — Environment is ready
- **Some issues found. See above for details.** (yellow) — Action needed

---

## pyfly db

Database migration commands powered by [Alembic](https://alembic.sqlalchemy.org/). These commands wrap Alembic's functionality with PyFly-specific defaults and Rich output.

The `migrate`, `upgrade`, and `downgrade` subcommands expect `alembic.ini` to exist in the current directory (created by `pyfly db init`).

All `pyfly db` subcommands require Alembic to be installed. If not available:

```
✗ alembic is not installed.
  Install the data-relational extra: pyfly[data-relational]
```

### pyfly db init

Initialize the Alembic migration environment in the current directory.

```bash
pyfly db init
```

**What it does:**

1. Creates an `alembic/` directory with Alembic's standard structure
2. Creates `alembic.ini` configuration file
3. **Overwrites `alembic/env.py`** with a PyFly-customized template that includes:
   - `async_engine_from_config` for async database support (asyncpg, aiosqlite)
   - `Base.metadata` from `pyfly.data.relational.sqlalchemy` as the target metadata for autogeneration
   - Support for both offline (SQL script) and online (async connection) migration modes

**Error handling:** If an `alembic/` directory already exists, the command exits with an error rather than overwriting.

```
✗ Directory 'alembic' already exists. Remove it first if you want to re-initialize.
```

### pyfly db migrate

Auto-generate a new migration revision by comparing your SQLAlchemy models to the current database state.

```bash
pyfly db migrate [-m "description"]
```

| Option | Description |
|--------|-------------|
| `-m`, `--message` | Revision message describing the changes |

**Examples:**
```bash
pyfly db migrate -m "add user table"
pyfly db migrate -m "add order status column"
```

This runs Alembic's `revision --autogenerate`, which:
1. Compares your current `Base.metadata` (all entity models) with the database
2. Generates upgrade/downgrade functions in a new version file under `alembic/versions/`

**Prerequisites:** `alembic.ini` must exist (run `pyfly db init` first).

### pyfly db upgrade

Apply pending migrations to bring the database up to a specific revision.

```bash
pyfly db upgrade [REVISION]
```

| Argument | Default | Description |
|----------|---------|-------------|
| `revision` | `head` | Target revision (use `head` for latest) |

**Examples:**
```bash
pyfly db upgrade           # Apply all pending migrations
pyfly db upgrade head      # Same as above (explicit)
pyfly db upgrade abc123    # Upgrade to specific revision
```

### pyfly db downgrade

Revert the database to a previous revision.

```bash
pyfly db downgrade REVISION
```

| Argument | Required | Description |
|----------|----------|-------------|
| `revision` | Yes | Target revision to downgrade to |

**Examples:**
```bash
pyfly db downgrade -1       # Revert one migration step
pyfly db downgrade abc123   # Revert to specific revision
pyfly db downgrade base     # Revert all migrations
```

### Running Migrations on Startup

Instead of (or in addition to) running `pyfly db upgrade` by hand, PyFly can apply migrations **automatically on application startup** — the Flyway-style auto-migrate equivalent. This reuses the same Alembic environment created by `pyfly db init`, so the CLI commands above continue to work unchanged.

It is **opt-in** via configuration, not a CLI flag. Enable it in `pyfly.yaml`:

```yaml
pyfly:
  data:
    relational:
      url: postgresql+asyncpg://user:pass@localhost:5432/app
      migrations:
        enabled: true            # run `alembic upgrade head` on startup
        config: alembic.ini      # Alembic config path (default: alembic.ini)
        revision: head           # target revision (default: head)
```

When enabled, the app runs `alembic upgrade <revision>` against the same datasource (`pyfly.data.relational.url`) during startup. If `alembic.ini` is not found, the migration step is skipped with a warning suggesting you run `pyfly db init` — startup is not aborted. See [Data Relational — Run Migrations on Startup](modules/data-relational.md#run-migrations-on-startup-flyway-style) for the full configuration reference.

---

## pyfly license

Display the Apache 2.0 license text for the PyFly Framework.

### Usage

```bash
pyfly license
```

The command first tries to read the `LICENSE` file from the installed package resources, then falls back to the project root filesystem. If no license file is found, it displays a summary with a link to the Apache 2.0 license.

---

## pyfly sbom

Display the Software Bill of Materials (SBOM) — a table of all PyFly dependencies with their required and installed versions.

### Usage

```bash
pyfly sbom [OPTIONS]
```

### Options

| Option | Description |
|--------|-------------|
| `--json` | Output as JSON instead of a Rich table |

### Output

The command displays a Rich table with three columns:

| Column | Description |
|--------|-------------|
| Package | Dependency name |
| Required | Version specifier from `pyproject.toml` |
| Installed | Installed version (green) or `not installed` (yellow) |

The JSON output includes the PyFly version, license, and full dependency list — useful for compliance and auditing pipelines.

### Examples

```bash
# Display as Rich table
pyfly sbom

# Export as JSON
pyfly sbom --json
```

---

## Typical Development Workflow

Here's a typical workflow using the CLI tools throughout the lifecycle of a PyFly project:

```bash
# 1. Check your environment is ready
pyfly doctor

# 2. Create a new project
pyfly new order-service
cd order-service

# 3. Verify framework info and installed extras
pyfly info

# 4. Initialize database migrations
pyfly db init

# 5. Create initial migration from your entity models
pyfly db migrate -m "initial schema"

# 6. Apply the migration
pyfly db upgrade

# 7. Start developing with auto-reload
pyfly run --reload

# 8. As you add/modify entities, generate new migrations
pyfly db migrate -m "add order status"
pyfly db upgrade

# 9. If you need to roll back a migration
pyfly db downgrade -1
```

---

## Extending the CLI

The CLI is built on Click, making it straightforward to add custom commands for your project. The entry point is defined in `pyproject.toml`:

```toml
[project.scripts]
pyfly = "pyfly.cli.main:cli"
```

To add custom commands, create a Click command and register it with the CLI group:

```python
import click
from pyfly.cli.main import cli

@click.command()
@click.argument("entity_name")
def generate_command(entity_name: str) -> None:
    """Generate boilerplate for a new entity."""
    click.echo(f"Generating {entity_name} entity, repository, and service...")

cli.add_command(generate_command, name="generate")
```
