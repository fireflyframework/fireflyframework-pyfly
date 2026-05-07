# Installation Guide

This guide covers every way to install PyFly — from the interactive installer for first-time users to manual uv/pip commands for CI/CD pipelines.

> **Note:** PyFly is distributed exclusively via GitHub as part of the [Firefly Framework](https://github.com/fireflyframework) organization. It is **not** published to PyPI. All installation methods require cloning the repository first.

---

## Table of Contents

- [Quick Install](#quick-install)
- [Prerequisites](#prerequisites)
- [Interactive Installation](#interactive-installation)
- [Non-Interactive Installation](#non-interactive-installation)
- [Manual Installation](#manual-installation)
- [Available Extras](#available-extras)
- [Core Dependencies](#core-dependencies)
- [What the Installer Does](#what-the-installer-does)
- [Development Setup](#development-setup)
- [Verifying Installation](#verifying-installation)
- [Configuration After Installation](#configuration-after-installation)
- [Troubleshooting](#troubleshooting)
- [Uninstalling](#uninstalling)

---

## Quick Install

```bash
# Clone the repository
git clone https://github.com/fireflyframework/fireflyframework-pyfly.git
cd pyfly

# Full installation (all modules)
bash install.sh

# Or with specific extras
PYFLY_EXTRAS=web,data-relational bash install.sh
```

After installation, verify with:

```bash
pyfly --version
pyfly doctor
```

---

## Prerequisites

| Requirement | Version | How to Check |
|-------------|---------|-------------|
| Python | >= 3.12 | `python3 --version` |
| uv | >= 0.5 (recommended) | `uv --version` |
| Git | Any recent version | `git --version` |
| pip | Latest (alternative to uv) | `pip --version` |
| OS | macOS or Linux | Windows support planned |

### Python 3.12+

PyFly requires Python 3.12 or later for modern type hint features (`type` statement, `TypeVar` improvements, and performance optimizations).

**macOS:**
```bash
brew install python@3.12
```

**Ubuntu/Debian:**
```bash
sudo apt update && sudo apt install python3.12 python3.12-venv
```

**Verify:**
```bash
python3 --version  # Should show 3.12.x or higher
```

---

## Interactive Installation

The installer script provides a guided experience for first-time installation.

### Running the Installer

From the PyFly source directory:

```bash
bash install.sh
```

### Prompts

The installer asks three questions:

**1. Installation Directory**

```
Where should PyFly be installed? [~/.pyfly]:
```

Default: `~/.pyfly`. This directory will contain the PyFly source code, a dedicated virtual environment, and the `pyfly` wrapper script.

**2. Extras Selection**

```
Which extras should be installed?
  1) full — All modules (recommended)
  2) web — Web framework (Starlette, uvicorn)
  3) data-relational — SQL Database (SQLAlchemy, Alembic)
  4) data-document — Document Database (MongoDB, Beanie ODM)
  5) eda — Event-Driven (Kafka, RabbitMQ)
  6) security — Auth & JWT
  7) custom — Enter comma-separated extras

Your choice [1]:
```

If you choose `custom`, you'll be prompted to enter a comma-separated list:

```
Enter extras (comma-separated): web,data-relational,security,cli
```

**3. PATH Configuration**

```
Add pyfly to PATH? [Y/n]:
```

If you answer `Y` (default), the installer appends a PATH entry to your shell profile (`.zshrc`, `.bashrc`, `.bash_profile`, or `.profile`):

```bash
export PATH="$HOME/.pyfly/bin:$PATH"  # PyFly Framework
```

### Example Session

```
$ bash install.sh

                _____.__
______ ___.__._/ ____\  | ___.__.
\____ <   |  |\   __\|  |<   |  |
|  |_> >___  | |  |  |  |_\___  |
|   __// ____| |__|  |____/ ____|
|__|   \/                 \/

PyFly Installer

Where should PyFly be installed? [~/.pyfly]:
Which extras? (1=full, 2=web, 3=data-relational, 4=data-document, 5=eda, 6=security, 7=custom) [1]: 1
Add pyfly to PATH? [Y/n]: Y

✓ Python 3.12.5 detected
✓ Source copied to ~/.pyfly
✓ Virtual environment created
✓ PyFly installed with [full] extras
✓ Wrapper script created at ~/.pyfly/bin/pyfly
✓ PATH updated in ~/.zshrc

Installation complete! Run 'pyfly doctor' to verify.
```

---

## Non-Interactive Installation

For CI/CD pipelines, Docker images, or automated setups, the installer detects when it's piped or when stdin is not a terminal, and uses sensible defaults without prompting.

### Pipe from GitHub

```bash
curl -fsSL https://raw.githubusercontent.com/fireflyframework/fireflyframework-pyfly/main/install.sh | bash
```

### Default Behavior (Non-Interactive)

When piped or when stdin is not a TTY:
- **Install directory:** `~/.pyfly` (or `PYFLY_HOME`)
- **Extras:** `full` (or `PYFLY_EXTRAS`)
- **PATH:** Automatically added

### Environment Variable Overrides

Customize any default with environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `PYFLY_HOME` | `~/.pyfly` | Installation directory |
| `PYFLY_EXTRAS` | `full` | Comma-separated extras to install |
| `PYFLY_SOURCE` | Current directory | Path to PyFly source |

### Examples

```bash
# Install to a custom directory
PYFLY_HOME=/opt/pyfly bash install.sh

# Install only web and data-relational modules
PYFLY_EXTRAS=web,data-relational bash install.sh

# Full customization
PYFLY_HOME=/opt/pyfly PYFLY_EXTRAS=web,data-relational,cli PYFLY_SOURCE=/src/pyfly bash install.sh

# CI/CD: non-interactive with full extras
curl -fsSL https://raw.githubusercontent.com/fireflyframework/fireflyframework-pyfly/main/install.sh | PYFLY_HOME=/app/pyfly bash
```

### Docker Example

```dockerfile
FROM python:3.12-slim

COPY pyfly-source/ /tmp/pyfly-src/
RUN cd /tmp/pyfly-src && \
    PYFLY_HOME=/opt/pyfly PYFLY_EXTRAS=web,data-relational bash install.sh && \
    rm -rf /tmp/pyfly-src

ENV PATH="/opt/pyfly/bin:${PATH}"

WORKDIR /app
COPY . .
CMD ["pyfly", "run", "--host", "0.0.0.0", "--port", "8080"]
```

---

## Manual Installation

If you prefer to manage your own virtual environment, you can install PyFly directly with uv or pip.

### From Source (uv — Recommended)

```bash
# Clone the repository
git clone https://github.com/fireflyframework/fireflyframework-pyfly.git
cd pyfly

# Install with all extras
uv sync --all-extras

# Or install with specific extras
uv sync --extra web --extra data-relational --extra cli

# Or install the bare minimum (core + pydantic + pyyaml)
uv sync
```

### From Source (pip)

```bash
# Clone the repository
git clone https://github.com/fireflyframework/fireflyframework-pyfly.git
cd pyfly

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install with all extras
pip install -e ".[full]"

# Or install with specific extras
pip install -e ".[web,data-relational,cli]"

# Or install the bare minimum (core + pydantic + pyyaml)
pip install -e .
```

### Extras Syntax

With uv:

```bash
uv add "pyfly[web]"                           # Web only
uv add "pyfly[web,data-relational]"           # Web + SQL data
uv add "pyfly[web,data-relational,security]"  # Web + SQL data + security
uv add "pyfly[full]"                          # Everything
```

With pip:

```bash
pip install -e ".[web]"                              # Web only
pip install -e ".[web,data-relational]"              # Web + SQL data
pip install -e ".[web,data-relational,security]"     # Web + SQL data + security
pip install -e ".[full]"                             # Everything
# Note: dev tools (pytest, mypy, ruff) are in [dependency-groups] — use: uv sync --group dev
```

---

## Available Extras

Each extra pulls in the third-party libraries needed for a specific framework module. Install only what your application needs to keep the dependency footprint minimal.

| Extra | Dependencies | What It Enables |
|-------|-------------|-----------------|
| `web` | starlette, uvicorn, python-multipart | HTTP server (Starlette + Uvicorn), REST controllers, routing, middleware, OpenAPI docs |
| `web-fast` | starlette, granian, uvloop, python-multipart | High-performance web stack: Starlette + Granian + uvloop |
| `web-fastapi` | fastapi, granian, uvloop, python-multipart | High-performance FastAPI stack: FastAPI + Granian + uvloop |
| `fastapi` | fastapi, uvicorn, python-multipart | HTTP server (FastAPI + Uvicorn), REST controllers, native OpenAPI |
| `granian` | granian | Granian ASGI server (Rust/tokio, ~3x faster than Uvicorn) |
| `hypercorn` | hypercorn | Hypercorn ASGI server (HTTP/2 and HTTP/3 support) |
| `data-relational` | sqlalchemy[asyncio], alembic, aiosqlite | Async SQL database access, repositories, migrations (SQLite default) |
| `data-document` | motor, beanie | MongoDB document access via Beanie ODM |
| `postgresql` | asyncpg | PostgreSQL async driver (add for production databases) |
| `eda` | aiokafka, aio-pika | Both Kafka and RabbitMQ message brokers |
| `kafka` | aiokafka | Apache Kafka messaging only |
| `rabbitmq` | aio-pika | RabbitMQ messaging only |
| `redis` | redis[hiredis] | Redis client (hiredis C parser for performance) |
| `cache` | redis[hiredis] | Caching with Redis backend |
| `client` | httpx | Resilient HTTP client with retry and circuit breaker |
| `observability` | prometheus-client, opentelemetry-api, opentelemetry-sdk, structlog | Metrics, distributed tracing, structured logging |
| `security` | pyjwt[crypto], bcrypt, cryptography | JWT token generation/verification, password hashing |
| `scheduling` | croniter | Cron expression parsing for scheduled tasks |
| `cli` | click, rich, jinja2, questionary | CLI tools (pyfly new, run, info, doctor, db) |
| `full` | All of the above | Complete framework with all modules |

### Choosing Extras

**For a typical web service:** `web,data-relational,security,cli`

**For a high-performance web service:** `web-fast,data-relational,security,cli`

**For a FastAPI service:** `web-fastapi,data-relational,security,cli`

**For a microservice with messaging:** `web,data-relational,eda,cache,cli`

**For development:** `uv sync --all-extras --group dev` (includes everything + test/lint tools)

**For production Docker images:** Only the extras your service actually uses (minimize image size)

### Auto-Configuration by Extras

PyFly's auto-configuration engine detects which libraries are installed and wires the appropriate adapters:

| Installed Library | Auto-Configured Adapter |
|------------------|------------------------|
| `fastapi` | `FastAPIWebAdapter` (instead of `StarletteWebAdapter`) |
| `granian` | `GranianServerAdapter` (instead of `UvicornServerAdapter`) |
| `uvloop` | `UvloopEventLoopAdapter` (instead of `AsyncioEventLoopAdapter`) |
| `redis` | `RedisCacheAdapter` (instead of `InMemoryCache`) |
| `aiokafka` | `KafkaAdapter` (instead of `InMemoryMessageBroker`) |
| `aio-pika` | `RabbitMQAdapter` (instead of `InMemoryMessageBroker`) |
| `prometheus-client` | Prometheus metrics endpoint enabled |
| `structlog` | Structured JSON logging (instead of stdlib logging) |

This means you can start development with zero configuration — `InMemoryCache` and `InMemoryMessageBroker` work immediately — and switch to production backends simply by installing the appropriate extra.

---

## Core Dependencies

These are always installed regardless of which extras you choose:

| Package | Version | Purpose |
|---------|---------|---------|
| `pydantic` | >= 2.0 | Data validation, serialization, config binding |
| `pyyaml` | >= 6.0 | YAML configuration file parsing |

PyFly has **zero additional runtime dependencies** beyond these two and the Python standard library. All other dependencies are optional and pulled in through extras.

---

## What the Installer Does

The `install.sh` script performs these steps:

### 1. Prerequisites Check

Verifies that Python >= 3.12, the `venv` module, and `pip` are available. If any check fails, the installer exits with a clear error message.

### 2. Source Copy

Copies the PyFly source tree to `$INSTALL_DIR/source` (default: `~/.pyfly/source`), then removes development artifacts: `.worktrees`, `.venv`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `htmlcov`, and `.coverage`.

### 3. Virtual Environment Creation

Creates an isolated Python virtual environment at `$INSTALL_DIR/venv`:

```bash
python3 -m venv "$INSTALL_DIR/venv"
```

This ensures PyFly's dependencies don't conflict with your system Python or other projects.

### 4. PyFly Installation

Runs an editable install inside the virtual environment (uses `uv` if available, falls back to `pip`):

```bash
# If uv is available:
uv pip install --python "$INSTALL_DIR/venv/bin/python" -e ".[${EXTRAS}]"
# Otherwise:
$INSTALL_DIR/venv/bin/pip install -e ".[${EXTRAS}]"
```

### 5. Wrapper Script

Creates an executable wrapper at `$INSTALL_DIR/bin/pyfly` that activates the virtual environment before running the CLI:

```bash
#!/usr/bin/env bash
# PyFly CLI wrapper — activates the venv and runs pyfly
exec "$HOME/.pyfly/venv/bin/pyfly" "$@"
```

### 6. PATH Configuration

Appends the `bin/` directory to your shell profile so `pyfly` is available globally:

```bash
export PATH="$HOME/.pyfly/bin:$PATH"  # PyFly Framework
```

### 7. Cleanup on Failure

If any step fails, the installer removes the partially-created installation directory to leave your system clean.

---

## Development Setup

For contributing to PyFly itself, install with dev dependencies which include all modules plus testing and linting tools:

```bash
git clone https://github.com/fireflyframework/fireflyframework-pyfly.git
cd pyfly

# With uv (recommended)
uv sync --all-extras --group dev

# Or with pip (dev tools must be installed separately)
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[full]"
pip install pytest pytest-asyncio pytest-cov mypy ruff
```

> **Note:** `uv sync` generates a `uv.lock` file. Commit this file to version control to ensure
> reproducible installs across all environments.

### Development Tools Included

| Tool | Purpose |
|------|---------|
| pytest | Test runner |
| pytest-asyncio | Async test support |
| pytest-cov | Coverage reporting |
| mypy | Static type checking (strict mode) |
| ruff | Fast linter and formatter |
| aiosqlite | In-memory SQLite for tests |

### Running Tests

```bash
# Run all tests
python -m pytest --tb=short -q

# Run with coverage
python -m pytest --cov=pyfly --cov-report=term-missing

# Run a specific module's tests
python -m pytest tests/web/ -v

# Type checking
mypy src/pyfly --strict

# Linting
ruff check src/ tests/
```

---

## Verifying Installation

After installation, run these commands to verify everything is working:

```bash
# Check the CLI version
pyfly --version

# Display environment and installed extras
pyfly info

# Run comprehensive health check
pyfly doctor
```

### Expected `pyfly doctor` Output

```
PyFly Doctor

  ✓ Python 3.12.5
  ✓ Virtual environment active

  Required tools:
    ✓ git — Version control
    ✓ uv — Package manager

  Optional tools:
    ✓ uvicorn — ASGI server (pyfly run)
    ✓ alembic — Database migrations (pyfly db)
    ✓ ruff — Linter & formatter
    ✓ mypy — Type checker

  PyFly packages:
    ✓ pyfly v26.05.01

  All checks passed!
```

---

## Configuration After Installation

After installing, create your first project:

```bash
pyfly new my-service
cd my-service
```

This generates a `pyfly.yaml` with sensible defaults. See the [Configuration Guide](modules/configuration.md) for detailed configuration options.

---

## Troubleshooting

### "command not found: pyfly"

The PATH wasn't updated. Either:
- Source your shell profile: `source ~/.zshrc` (or `~/.bashrc`)
- Or add the PATH manually: `export PATH="$HOME/.pyfly/bin:$PATH"`

### "Python 3.12 required but found 3.11"

Install Python 3.12+:
- macOS: `brew install python@3.12`
- Ubuntu: `sudo apt install python3.12 python3.12-venv`

### "ModuleNotFoundError: No module named 'starlette'"

The `web` extra isn't installed. Reinstall with:
```bash
uv sync --extra web
```

Or reinstall everything:
```bash
uv sync --all-extras
```

### "venv module not found"

On some Linux distributions, `venv` is a separate package:
```bash
sudo apt install python3.12-venv
```

### Installation hangs or fails

If using pip, try upgrading it first:
```bash
python3 -m pip install --upgrade pip
```

If using uv, try clearing the cache:
```bash
uv cache clean
```

---

## Uninstalling

### Installer-based Installation

1. Remove the installation directory:
```bash
rm -rf ~/.pyfly
```

2. Remove the PATH entry from your shell profile (`.zshrc`, `.bashrc`, etc.) — look for the line containing `# PyFly Framework`:
```bash
# Remove this line:
export PATH="$HOME/.pyfly/bin:$PATH"  # PyFly Framework
```

### Manual Installation

```bash
uv remove pyfly
# Or with pip:
pip uninstall pyfly
```
