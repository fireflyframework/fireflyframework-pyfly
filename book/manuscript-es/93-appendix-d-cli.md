<span class="eyebrow">Apéndice D</span>

# CLI y resolución de problemas {.chtitle}

La CLI `pyfly` proporciona andamiaje de proyectos, arranque de la aplicación,
migraciones de base de datos y diagnóstico del entorno. Está construida con
**Click** para el análisis de comandos y con **Rich** para la salida coloreada en
la terminal.

**Instalación:** `uv add "pyfly[cli]"` (o `uv sync --extra cli`). Requiere Click,
Rich, Jinja2 y questionary.

**Punto de entrada:** `pyfly` — registrado como script de consola en `pyproject.toml`.

---

## Referencia de comandos

| Comando | Descripción |
|---|---|
| `pyfly new [NAME]` | Genera el andamiaje de un nuevo proyecto (interactivo o directo) |
| `pyfly run` | Arranca el servidor de aplicación ASGI |
| `pyfly info` | Muestra la versión de Python y los extras instalados |
| `pyfly doctor` | Diagnostica el entorno (Python, herramientas, PyFly) |
| `pyfly db init` | Inicializa un entorno de migraciones de Alembic |
| `pyfly db migrate [-m MSG]` | Genera automáticamente una revisión de migración |
| `pyfly db upgrade [REVISION]` | Aplica las migraciones pendientes (por defecto: `head`) |
| `pyfly db downgrade REVISION` | Revierte a una revisión anterior |
| `pyfly license` | Muestra el texto de la licencia Apache 2.0 |
| `pyfly sbom [--json]` | Tabla del Software Bill of Materials (inventario de software) |
| `pyfly --version` | Imprime la versión instalada de PyFly |
| `pyfly --help` | Imprime el banner y todos los comandos |

---

## pyfly new

Crea un directorio de proyecto completo a partir de uno de siete arquetipos.

```
pyfly new <name> [--archetype ARCHETYPE] [--features FEAT,...] [--directory DIR]
pyfly new                              # interactive mode (questionary TUI)
```

### Arquetipos

| Arquetipo | Características por defecto | Qué se genera |
|---|---|---|
| `core` | *(ninguna)* | Contenedor de DI, configuración, Dockerfile |
| `web-api` | `web` | Controladores REST, servicios, repositorios (CRUD de Todo) |
| `fastapi-api` | `fastapi` | Stack FastAPI, OpenAPI nativo |
| `web` | `web` | HTML renderizado en servidor con plantillas Jinja2 |
| `hexagonal` | `web` | Puertos y adaptadores, capas dominio/aplicación/infra/api |
| `library` | *(ninguna)* | Paquete PEP 561 `py.typed`, sin runtime de PyFly |
| `cli` | `shell` | Comandos de shell con DI, sin punto de entrada ASGI |

### Valores disponibles para `--features`

| Característica | Qué añade |
|---|---|
| `web` | Servidor HTTP Starlette, controladores REST, documentación OpenAPI |
| `fastapi` | Servidor HTTP FastAPI, OpenAPI nativo |
| `granian` | Servidor ASGI Granian (Rust/tokio) |
| `hypercorn` | Servidor ASGI Hypercorn (HTTP/2, HTTP/3) |
| `data-relational` | SQLAlchemy ORM (asíncrono), migraciones Alembic |
| `data-document` | Beanie ODM, Motor (MongoDB) |
| `eda` | Bus de eventos en memoria, soporte de Kafka + RabbitMQ |
| `cache` | Capa de caché (en memoria por defecto; Redis si está instalado) |
| `client` | Cliente HTTP resiliente (httpx + reintentos/cortacircuitos) |
| `security` | Autenticación JWT, hashing de contraseñas con bcrypt |
| `scheduling` | Programación de tareas basada en cron |
| `observability` | Métricas de Prometheus, trazas de OpenTelemetry |
| `cqrs` | Segregación de Responsabilidad entre Comandos y Consultas (CQRS) |
| `shell` | Comandos de shell interactivos potenciados con DI |

### Ejemplos

::: listing terminal | Listado D.1 — Ejemplos de pyfly new
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

En el modo interactivo, el asistente solicita el nombre, el nombre del paquete
(precargado a partir del nombre del proyecto), el arquetipo (selección única con
las flechas) y las características (selección múltiple con la barra espaciadora).
Antes de la creación se muestra un resumen de confirmación. Pulsa Ctrl+C en
cualquier prompt para salir de forma limpia.

Los nombres de proyecto que contienen guiones se convierten automáticamente a
nombres de paquete de Python válidos (`my-service` → `my_service`). Usa el prompt
**Package name** en el modo interactivo para sobrescribir este valor derivado.

---

## pyfly run

Arranca la aplicación ASGI usando el servidor seleccionado automáticamente.

```
pyfly run [--host HOST] [--port PORT] [--server SERVER]
          [--workers N] [--reload] [--app MODULE:VAR]
```

| Opción | Por defecto | Descripción |
|---|---|---|
| `--host` | `0.0.0.0` | Dirección de enlace |
| `--port` | Config o `8080` | Puerto de la app (CLI → `pyfly.server.port` → 8080) |
| `--server` | Autodetección | `granian`, `uvicorn` o `hypercorn` |
| `--workers` | Config o `0` | Procesos worker (`0` = número de CPUs) |
| `--reload` | desactivado | Recarga automática al cambiar el código |
| `--app` | Autodescubrimiento | Ruta de importación, p. ej. `myapp.main:app` |

**Prioridad de servidores** (cuando se omite `--server`): Granian › Uvicorn › Hypercorn.
Gana el primer servidor que se pueda importar.

**Descubrimiento de la aplicación** (cuando se omite `--app`):

1. `pyfly.app.module` en `pyfly.yaml`
2. `app.module` en `pyfly.yaml` (disposición plana)
3. Autoescaneo de `src/<package>/main.py`

`pyfly run` añade `src/` a `sys.path` automáticamente en los proyectos con
disposición src.

::: listing terminal | Listado D.2 — Ejemplos de pyfly run
# Development with auto-reload
pyfly run --reload

# Production: Granian, bind all interfaces, all CPU cores
pyfly run --host 0.0.0.0 --port 80 --server granian --workers 0

# Explicit application path
pyfly run --app my_service.main:app
:::

---

## pyfly info

Muestra dos tablas de Rich: versión de Python, plataforma y arquitectura; además
del estado de instalación de cada módulo opcional de PyFly. Resulta útil tras una
instalación selectiva para confirmar qué adaptadores están disponibles.

```
pyfly info
```

---

## pyfly doctor

Ejecuta una comprobación integral de salud del entorno.

```
pyfly doctor
```

| Comprobación | Condición de aprobado | Impacto del fallo |
|---|---|---|
| Versión de Python | >= 3.12 | Fatal — doctor informa de fallo |
| Entorno virtual | `sys.prefix != sys.base_prefix` | Solo advertencia |
| `git` en el PATH | `shutil.which("git")` devuelve una ruta | Fatal |
| `uv` en el PATH | `shutil.which("uv")` devuelve una ruta | Fatal |
| `uvicorn` en el PATH | presente | Informativo (`-`) |
| `alembic` en el PATH | presente | Informativo (`-`) |
| `ruff` en el PATH | presente | Informativo (`-`) |
| `mypy` en el PATH | presente | Informativo (`-`) |
| PyFly importable | `import pyfly` tiene éxito | Fatal |

Finaliza con **"All checks passed!"** o **"Some issues found."**.

---

## pyfly db

Comandos de migración de base de datos respaldados por [Alembic](https://alembic.sqlalchemy.org/).

!!! note "Requiere el extra data-relational"
    Todos los subcomandos `pyfly db` requieren Alembic (`pyfly[data-relational]`).

### pyfly db init

```
pyfly db init
```

Crea `alembic/` y `alembic.ini` en el directorio actual. Sobrescribe
`alembic/env.py` con una plantilla de PyFly que conecta `async_engine_from_config`
y `Base.metadata` de `pyfly.data.relational.sqlalchemy`. Finaliza con un error si
`alembic/` ya existe.

### pyfly db migrate

```
pyfly db migrate [-m "description"]
```

Ejecuta `alembic revision --autogenerate`. Compara el `Base.metadata` actual con
la base de datos en vivo y escribe un nuevo archivo de versión en
`alembic/versions/`. Requiere `alembic.ini` (ejecuta primero `pyfly db init`).

### pyfly db upgrade / downgrade

```
pyfly db upgrade [REVISION]    # default: head
pyfly db downgrade REVISION    # e.g. -1, base, abc123
```

### Flujo de trabajo típico de migración

::: listing terminal | Listado D.3 — Ciclo de vida de las migraciones de base de datos
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

Imprime una tabla de Rich con cada dependencia de PyFly junto con las versiones
requeridas e instaladas. La opción `--json` produce JSON legible por máquina para
pipelines de cumplimiento normativo.

```
pyfly sbom
pyfly sbom --json
```

---

## Flujo de trabajo de desarrollo típico

::: listing terminal | Listado D.4 — Flujo de trabajo de extremo a extremo, de la configuración al desarrollo
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

## Resolución de problemas

### Problemas comunes y soluciones

| Síntoma | Causa probable | Solución |
|---|---|---|
| `command not found: pyfly` | El PATH no se actualizó tras la instalación | `source ~/.zshrc` (o `~/.bashrc`); o `export PATH="$HOME/.pyfly/bin:$PATH"` |
| `Python 3.11 found (3.12 required)` | Python antiguo en el PATH | `brew install python@3.12` (macOS) o `sudo apt install python3.12` (Ubuntu) |
| `ModuleNotFoundError: No module named 'starlette'` | El extra `web` no está instalado | `uv add "pyfly[web]"` o `uv sync --extra web` |
| `ModuleNotFoundError: No module named 'beanie'` | Falta el extra `data-document` | `uv add "pyfly[data-document]"` |
| `ModuleNotFoundError: No module named 'sqlalchemy'` | Falta el extra `data-relational` | `uv add "pyfly[data-relational]"` |
| `alembic is not installed` | Falta Alembic | `uv add "pyfly[data-relational]"` |
| `No application found` | No hay `pyfly.yaml` ni opción `--app` | Añade `pyfly.app.module: myapp.main:app` a `pyfly.yaml`, o pasa `--app myapp.main:app` |
| `No ASGI server found` | No hay ningún extra de servidor instalado | `uv add "pyfly[web]"` (añade Uvicorn) |
| `venv module not found` | Python en paquetes separados (Debian/Ubuntu) | `sudo apt install python3.12-venv` |
| `Directory 'alembic' already exists` | `db init` ejecutado dos veces | `rm -rf alembic alembic.ini` y vuelve a ejecutar `pyfly db init` |
| `uv cache clean` arregla instalaciones lentas | Caché de uv corrupta | Ejecuta `uv cache clean` y reintenta |
| Ctrl+C en `pyfly new` no deja archivos | Modo interactivo cancelado limpiamente | Vuelve a ejecutar `pyfly new`; no hace falta limpiar nada |

### Desinstalación

Para una instalación basada en el instalador (`bash install.sh`):

::: listing terminal | Listado D.5 — Desinstalar PyFly
# Remove the installation directory
rm -rf ~/.pyfly

# Remove the PATH line from your shell profile
# Delete the line: export PATH="$HOME/.pyfly/bin:$PATH"  # PyFly Framework
:::

Para una instalación manual con `uv`/`pip`: `uv remove pyfly` o `pip uninstall pyfly`.

---

## Extender la CLI

La CLI está construida sobre Click. Añade comandos personalizados importando el
grupo `cli` y llamando a `cli.add_command`:

::: listing myapp/generate.py | Listado D.6 — Añadir un comando de CLI personalizado
import click
from pyfly.cli.main import cli


@click.command()
@click.argument("entity_name")
def generate(entity_name: str) -> None:
    """Generate boilerplate for a new entity."""
    click.echo(f"Generating {entity_name} entity ...")


cli.add_command(generate, name="generate")
:::

Registra el módulo del comando en los entry points de tu `pyproject.toml` o
impórtalo en el `__init__.py` de tu aplicación antes de que se invoque la CLI.
