<span class="eyebrow">Appendix D</span>

# CLI & Troubleshooting {.chtitle}

The `pyfly` CLI provides project scaffolding, application startup, database
migrations, and environment diagnostics. It is built with **Click** for command
parsing and **Rich** for coloured terminal output.

**Install:** `uv add "pyfly[cli]"` (or `uv sync --extra cli`). Requires Click,
Rich, Jinja2, and questionary.

**Entry point:** `pyfly` — registered as a console script in `pyproject.toml`.

---

## Command reference

| Command | Description |
|---|---|
| `pyfly new [NAME]` | Scaffold a new project (interactive or direct) |
| `pyfly run` | Start the ASGI application server |
| `pyfly info` | Show Python version and installed extras |
| `pyfly doctor` | Diagnose environment (Python, tools, PyFly) |
| `pyfly db init` | Initialise an Alembic migration environment |
| `pyfly db migrate [-m MSG]` | Auto-generate a migration revision |
| `pyfly db upgrade [REVISION]` | Apply pending migrations (default: `head`) |
| `pyfly db downgrade REVISION` | Revert to a previous revision |
| `pyfly license` | Display the Apache 2.0 license text |
| `pyfly sbom [--json]` | Software Bill of Materials table |
| `pyfly --version` | Print the installed PyFly version |
| `pyfly --help` | Print the banner and all commands |

---

## pyfly new

Creates a complete project directory from one of seven archetypes.

```
pyfly new <name> [--archetype ARCHETYPE] [--features FEAT,...] [--directory DIR]
pyfly new                              # interactive mode (questionary TUI)
```

### Archetypes

| Archetype | Default features | What is generated |
|---|---|---|
| `core` | *(none)* | DI container, config, Dockerfile |
| `web-api` | `web` | REST controllers, services, repositories (Todo CRUD) |
| `fastapi-api` | `fastapi` | FastAPI stack, native OpenAPI |
| `web` | `web` | Server-rendered HTML with Jinja2 templates |
| `hexagonal` | `web` | Ports & adapters, domain/application/infra/api layers |
| `library` | *(none)* | PEP 561 `py.typed` package, no PyFly runtime |
| `cli` | `shell` | Shell commands with DI, no ASGI entry point |

### Available `--features` values

| Feature | What it adds |
|---|---|
| `web` | Starlette HTTP server, REST controllers, OpenAPI docs |
| `fastapi` | FastAPI HTTP server, native OpenAPI |
| `granian` | Granian ASGI server (Rust/tokio) |
| `hypercorn` | Hypercorn ASGI server (HTTP/2, HTTP/3) |
| `data-relational` | SQLAlchemy ORM (async), Alembic migrations |
| `data-document` | Beanie ODM, Motor (MongoDB) |
| `eda` | In-memory event bus, Kafka + RabbitMQ support |
| `cache` | Caching layer (in-memory default; Redis if installed) |
| `client` | Resilient HTTP client (httpx + retry/circuit-breaker) |
| `security` | JWT authentication, bcrypt password hashing |
| `scheduling` | Cron-based task scheduling |
| `observability` | Prometheus metrics, OpenTelemetry tracing |
| `cqrs` | Command/Query Responsibility Segregation |
| `shell` | DI-powered interactive shell commands |

### Examples

::: listing terminal | Listing D.1 — pyfly new examples
# Minimal core service
pyfly new wallet-service

# REST API with SQL data layer
pyfly new lumen --archetype web-api --features web,data-relational

# Hexagonal service with cache and security
pyfly new payment-svc --archetype hexagonal \
    --features web,data-relational,cache,security

# MongoDB-backed microservice
pyfly new catalog-svc --archetype web-api \
    --features web,data-document

# CLI application with interactive shell
pyfly new admin-tool --archetype cli

# Interactive mode (arrow keys + checkboxes)
pyfly new
:::

In interactive mode the wizard prompts for name, package name (pre-filled from
the project name), archetype (single-select with arrows), and features
(multi-select with space bar). A confirmation summary is shown before creation.
Press Ctrl+C at any prompt to exit cleanly.

Project names containing hyphens are automatically converted to valid Python
package names (`my-service` → `my_service`). Use the **Package name** prompt in
interactive mode to override this derived value.

---

## pyfly run

Starts the ASGI application using the auto-selected server.

```
pyfly run [--host HOST] [--port PORT] [--server SERVER]
          [--workers N] [--reload] [--app MODULE:VAR]
```

| Option | Default | Description |
|---|---|---|
| `--host` | `0.0.0.0` | Bind address |
| `--port` | Config or `8080` | App port (CLI → `pyfly.server.port` → 8080) |
| `--server` | Auto-detect | `granian`, `uvicorn`, or `hypercorn` |
| `--workers` | Config or `0` | Worker processes (`0` = cpu count) |
| `--reload` | off | Auto-reload on code changes |
| `--app` | Auto-discovered | Import path, e.g. `myapp.main:app` |

**Server priority** (when `--server` is omitted): Granian › Uvicorn › Hypercorn.
The first importable server wins.

**Application discovery** (when `--app` is omitted):

1. `pyfly.app.module` in `pyfly.yaml`
2. `app.module` in `pyfly.yaml` (flat layout)
3. `src/<package>/main.py` auto-scan

`pyfly run` adds `src/` to `sys.path` automatically for src-layout projects.

::: listing terminal | Listing D.2 — pyfly run examples
# Development with auto-reload
pyfly run --reload

# Production: Granian, bind all interfaces, all CPU cores
pyfly run --host 0.0.0.0 --port 80 --server granian --workers 0

# Explicit application path
pyfly run --app my_service.main:app
:::

---

## pyfly info

Displays two Rich tables: Python version, platform, and architecture; plus
installation status of every PyFly optional module. Useful after a selective
install to confirm which adapters are available.

```
pyfly info
```

---

## pyfly doctor

Runs a comprehensive environment health check.

```
pyfly doctor
```

| Check | Pass condition | Failure impact |
|---|---|---|
| Python version | >= 3.12 | Fatal — doctor reports fail |
| Virtual environment | `sys.prefix != sys.base_prefix` | Warning only |
| `git` on PATH | `shutil.which("git")` returns a path | Fatal |
| `uv` on PATH | `shutil.which("uv")` returns a path | Fatal |
| `uvicorn` on PATH | present | Advisory (`-`) |
| `alembic` on PATH | present | Advisory (`-`) |
| `ruff` on PATH | present | Advisory (`-`) |
| `mypy` on PATH | present | Advisory (`-`) |
| PyFly importable | `import pyfly` succeeds | Fatal |

Exits with **"All checks passed!"** or **"Some issues found."**.

---

## pyfly db

Database migration commands backed by [Alembic](https://alembic.sqlalchemy.org/).

!!! note "Requires data-relational extra"
    All `pyfly db` subcommands require Alembic (`pyfly[data-relational]`).

### pyfly db init

```
pyfly db init
```

Creates `alembic/` and `alembic.ini` in the current directory. Overwrites
`alembic/env.py` with a PyFly template that wires `async_engine_from_config`
and `Base.metadata` from `pyfly.data.relational.sqlalchemy`. Exits with an error
if `alembic/` already exists.

### pyfly db migrate

```
pyfly db migrate [-m "description"]
```

Runs `alembic revision --autogenerate`. Compares current `Base.metadata` against
the live database and writes a new version file to `alembic/versions/`.
Requires `alembic.ini` (run `pyfly db init` first).

### pyfly db upgrade / downgrade

```
pyfly db upgrade [REVISION]    # default: head
pyfly db downgrade REVISION    # e.g. -1, base, abc123
```

### Typical migration workflow

::: listing terminal | Listing D.3 — Database migration lifecycle
# 1. Initialise Alembic (once per project)
pyfly db init

# 2. Generate initial migration from your entity models
pyfly db migrate -m "initial schema"

# 3. Apply migrations
pyfly db upgrade

# 4. After modifying entities, generate a new revision
pyfly db migrate -m "add order status column"
pyfly db upgrade

# 5. Roll back one step
pyfly db downgrade -1

# 6. Revert all migrations
pyfly db downgrade base
:::

---

## pyfly sbom

Prints a Rich table of every PyFly dependency with required and installed
versions. The `--json` flag outputs machine-readable JSON for compliance
pipelines.

```
pyfly sbom
pyfly sbom --json
```

---

## Typical development workflow

::: listing terminal | Listing D.4 — End-to-end workflow from setup to development
# Verify environment
pyfly doctor

# Scaffold the project
pyfly new lumen --archetype web-api \
    --features web,data-relational,security

cd lumen

# Check what was installed
pyfly info

# Initialise migrations
pyfly db init
pyfly db migrate -m "initial schema"
pyfly db upgrade

# Develop with auto-reload
pyfly run --reload

# Add a new column, migrate, upgrade
pyfly db migrate -m "add shipment_id to orders"
pyfly db upgrade
:::

---

## Troubleshooting

### Common problems and fixes

| Symptom | Likely cause | Fix |
|---|---|---|
| `command not found: pyfly` | PATH not updated after install | `source ~/.zshrc` (or `~/.bashrc`); or `export PATH="$HOME/.pyfly/bin:$PATH"` |
| `Python 3.11 found (3.12 required)` | Old Python on PATH | `brew install python@3.12` (macOS) or `sudo apt install python3.12` (Ubuntu) |
| `ModuleNotFoundError: No module named 'starlette'` | `web` extra not installed | `uv add "pyfly[web]"` or `uv sync --extra web` |
| `ModuleNotFoundError: No module named 'beanie'` | `data-document` extra missing | `uv add "pyfly[data-document]"` |
| `ModuleNotFoundError: No module named 'sqlalchemy'` | `data-relational` extra missing | `uv add "pyfly[data-relational]"` |
| `alembic is not installed` | Missing Alembic | `uv add "pyfly[data-relational]"` |
| `No application found` | No `pyfly.yaml` or `--app` flag | Add `pyfly.app.module: myapp.main:app` to `pyfly.yaml`, or pass `--app myapp.main:app` |
| `No ASGI server found` | No server extra installed | `uv add "pyfly[web]"` (adds Uvicorn) |
| `venv module not found` | Split-package Python (Debian/Ubuntu) | `sudo apt install python3.12-venv` |
| `Directory 'alembic' already exists` | `db init` run twice | `rm -rf alembic alembic.ini` then re-run `pyfly db init` |
| `uv cache clean` fixes slow installs | Corrupt uv cache | Run `uv cache clean` then retry |
| Ctrl+C in `pyfly new` leaves no files | Interactive mode cancelled cleanly | Re-run `pyfly new`; no cleanup needed |

### Uninstalling

For an installer-based installation (`bash install.sh`):

::: listing terminal | Listing D.5 — Uninstall PyFly
# Remove the installation directory
rm -rf ~/.pyfly

# Remove the PATH line from your shell profile
# Delete the line: export PATH="$HOME/.pyfly/bin:$PATH"  # PyFly Framework
:::

For a manual `uv`/`pip` installation: `uv remove pyfly` or `pip uninstall pyfly`.

---

## Extending the CLI

The CLI is built on Click. Add custom commands by importing the `cli` group and
calling `cli.add_command`:

::: listing myapp/generate.py | Listing D.6 — Adding a custom CLI command
import click
from pyfly.cli.main import cli


@click.command()
@click.argument("entity_name")
def generate(entity_name: str) -> None:
    """Generate boilerplate for a new entity."""
    click.echo(f"Generating {entity_name} entity ...")


cli.add_command(generate, name="generate")
:::

Register the command module in your `pyproject.toml` entry points or import it
in your application's `__init__.py` before the CLI is invoked.
