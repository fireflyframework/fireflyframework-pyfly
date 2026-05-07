# Getting Started with PyFly

This step-by-step tutorial walks you through building your first PyFly application
from scratch. By the end, you will have a working REST API with dependency injection,
structured error handling, and auto-generated API documentation.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Installation](#installation)
3. [Step 1: Create a New Project](#step-1-create-a-new-project)
4. [Step 2: Understand the Project Structure](#step-2-understand-the-project-structure)
5. [Step 3: The Application Class](#step-3-the-application-class)
6. [Step 4: Configuration with pyfly.yaml](#step-4-configuration-with-pyflyyaml)
7. [Step 5: Create a REST Controller](#step-5-create-a-rest-controller)
8. [Step 6: Create a Service Layer](#step-6-create-a-service-layer)
9. [Step 7: Wire Service to Controller](#step-7-wire-service-to-controller)
10. [Step 8: Add Error Handling](#step-8-add-error-handling)
11. [Step 9: Run the Application](#step-9-run-the-application)
12. [Step 10: Test the API](#step-10-test-the-api)
13. [Step 11: Add a Repository](#step-11-add-a-repository)
14. [Step 12: Check Your Environment](#step-12-check-your-environment)
15. [What's Next](#whats-next)

---

## Prerequisites

Before you begin, make sure you have:

- **Python 3.12 or later** -- PyFly uses modern Python features including type
  hints with generics, `match` statements, and `tomllib`.
- **uv** package manager ([install uv](https://docs.astral.sh/uv/)) or **pip** (included with Python).
- A **terminal** (macOS Terminal, Linux shell, or Windows PowerShell).
- Optionally, a code editor like VS Code, PyCharm, or Neovim.

Verify your Python version:

```bash
python --version
# Python 3.12.0 or later
```

---

## Installation

Install PyFly using the interactive installer:

```bash
bash install.sh
```

Or with environment variables for non-interactive installation:

```bash
PYFLY_EXTRAS=full bash install.sh
```

For detailed installation options including extras and virtual environments, see the
[Installation Guide](installation.md).

After installation, verify the CLI is available:

```bash
pyfly --version
```

---

## Step 1: Create a New Project

Use the PyFly CLI to scaffold a new project:

```bash
pyfly new my-service
cd my-service
```

This generates a complete project structure with sensible defaults.

> **Tip:** Run `pyfly new` without arguments to enter interactive mode, which guides
> you through archetype and feature selection with arrow-key navigation.

---

## Step 2: Understand the Project Structure

The scaffolded project looks like this:

```
my-service/
+-- pyproject.toml          # Python project metadata and dependencies
+-- pyfly.yaml              # PyFly configuration
+-- src/
|   +-- my_service/
|       +-- __init__.py
|       +-- app.py          # Application entry point
+-- tests/
    +-- __init__.py
```

| File/Directory    | Purpose                                           |
|-------------------|---------------------------------------------------|
| `pyproject.toml`  | Project metadata, dependencies, build settings    |
| `pyfly.yaml`      | Application configuration (ports, logging, etc.)  |
| `src/my_service/` | Your application code                             |
| `app.py`          | The `@pyfly_application` entry point              |
| `tests/`          | Your test files                                   |

As your application grows, you will add more files following this convention:

```
src/my_service/
+-- app.py              # Application entry point
+-- controllers.py      # REST controllers (HTTP layer)
+-- services.py         # Business logic (service layer)
+-- repositories.py     # Data access (repository layer)
+-- models.py           # Pydantic request/response models
```

---

## Step 3: The Application Class

Open `src/my_service/app.py`. This is your application's entry point:

```python
from pyfly.core import pyfly_application, PyFlyApplication


@pyfly_application(
    name="my-service",
    version="0.1.0",
    scan_packages=["my_service"],
    description="My first PyFly service",
)
class Application:
    pass
```

The `@pyfly_application` decorator marks this class as the PyFly application entry
point. It sets metadata attributes that the framework uses during startup.

**Parameters:**

| Parameter       | Type              | Default   | Description                              |
|-----------------|-------------------|-----------|------------------------------------------|
| `name`          | `str`             | required  | Application name (used in logs and info) |
| `version`       | `str`             | `"0.1.0"` | Application version                      |
| `scan_packages` | `list[str] \| None` | `None`  | Packages to scan for beans (components, services, controllers) |
| `description`   | `str`             | `""`      | Application description                  |

The `scan_packages` parameter is critical: it tells PyFly where to look for classes
decorated with `@component`, `@service`, `@repository`, `@rest_controller`, and
`@configuration`. Without it, your beans will not be auto-discovered.

`PyFlyApplication` is the bootstrap class that wires everything together. Its
startup sequence:

1. Load configuration from `pyfly.yaml` (with profile merging).
2. Configure structured logging.
3. Print the startup banner.
4. Scan packages for annotated beans.
5. Initialize the `ApplicationContext` (profile filtering, ordering, bean creation).
6. Log startup completion with timing.

---

## Step 4: Configuration with pyfly.yaml

Open `pyfly.yaml` in the project root:

```yaml
pyfly:
  app:
    name: my-service
    version: 0.1.0
    description: My first PyFly service

  profiles:
    active: ""                  # Set to "dev", "prod", etc.

  web:
    port: 8080
    host: "0.0.0.0"
    debug: false
    docs:
      enabled: true             # Swagger UI and ReDoc
    actuator:
      enabled: false            # Health check endpoints

  logging:
    level:
      root: INFO
    format: console             # "console" for dev, "json" for prod
```

PyFly uses a layered configuration system with this priority (highest wins):

| Priority | Source                              | Example                          |
|----------|-------------------------------------|----------------------------------|
| 4 (highest) | Environment variables            | `PYFLY_WEB_PORT=9000`          |
| 3        | Profile-specific config             | `pyfly-production.yaml`         |
| 2        | Your `pyfly.yaml`                   | `pyfly.yaml`                    |
| 1 (lowest) | Framework defaults                | Built-in `pyfly-defaults.yaml`  |

The framework defaults provide sensible values for every setting, so your
`pyfly.yaml` only needs to override what you want to change.

---

## Step 5: Create a REST Controller

Create `src/my_service/controllers.py`:

```python
from pyfly.container import rest_controller
from pyfly.web import request_mapping, get_mapping, post_mapping, Body


@rest_controller
@request_mapping("/api/greetings")
class GreetingController:

    @get_mapping("")
    async def list_greetings(self) -> list[dict]:
        """List all greetings."""
        return [
            {"id": 1, "message": "Hello, World!"},
            {"id": 2, "message": "Bonjour, le monde!"},
        ]

    @get_mapping("/{name}")
    async def greet(self, name: str) -> dict:
        """Greet someone by name."""
        return {"message": f"Hello, {name}!"}
```

Let's break down each element:

**`@rest_controller`** -- This stereotype decorator does two things:
1. Marks the class as a managed bean (so the DI container knows about it).
2. Sets the stereotype to `rest_controller` (JSON responses by default).

**`@request_mapping("/api/greetings")`** -- Sets the base URL path for all handler
methods in this controller.

**`@get_mapping("")`** -- Maps `GET /api/greetings` to the `list_greetings` method.

**`@get_mapping("/{name}")`** -- Maps `GET /api/greetings/{name}` to the `greet`
method. The `{name}` path variable is automatically extracted and passed as the
`name` parameter.

PyFly provides mapping decorators for all standard HTTP methods:

| Decorator          | HTTP Method | Example                        |
|--------------------|-------------|--------------------------------|
| `@get_mapping`     | GET         | `@get_mapping("/{id}")`        |
| `@post_mapping`    | POST        | `@post_mapping("", status_code=201)` |
| `@put_mapping`     | PUT         | `@put_mapping("/{id}")`        |
| `@patch_mapping`   | PATCH       | `@patch_mapping("/{id}")`      |
| `@delete_mapping`  | DELETE      | `@delete_mapping("/{id}")`     |

---

## Step 6: Create a Service Layer

Create `src/my_service/services.py`:

```python
from pyfly.container import service


@service
class GreetingService:
    """Business logic for greetings."""

    _greetings = {
        "world": "Hello, World! Welcome to PyFly.",
        "python": "Hello, Pythonista! PyFly loves Python.",
    }

    async def get_greeting(self, name: str) -> str:
        """Get a personalized greeting."""
        key = name.lower()
        if key in self._greetings:
            return self._greetings[key]
        return f"Hello, {name}! Welcome to PyFly."

    async def list_greetings(self) -> list[dict]:
        """List all available greetings."""
        return [
            {"name": name, "message": message}
            for name, message in self._greetings.items()
        ]
```

**`@service`** marks this class as a service-layer bean. Like `@rest_controller`, it
registers the class with the DI container. The difference is semantic: services
contain business logic, controllers handle HTTP concerns.

All stereotypes available in PyFly:

| Stereotype         | Purpose                              | Layer          |
|--------------------|--------------------------------------|----------------|
| `@component`       | Generic managed bean                 | Any            |
| `@service`         | Business logic                       | Service        |
| `@repository`      | Data access                          | Data           |
| `@controller`      | Web controller (template responses)  | Web            |
| `@rest_controller` | REST controller (JSON responses)     | Web            |
| `@configuration`   | Configuration class with `@bean` methods | Infrastructure |

Every stereotype supports these optional parameters:

```python
@service(
    name="greeting-service",    # Named bean (for @Qualifier lookup)
    scope=Scope.SINGLETON,      # SINGLETON (default), TRANSIENT, or REQUEST
    profile="dev",              # Only active in this profile
    condition=lambda: True,     # Conditional registration
)
class GreetingService:
    ...
```

---

## Step 7: Wire Service to Controller

Now inject the `GreetingService` into the controller using **constructor injection**.
Update `src/my_service/controllers.py`:

```python
from pyfly.container import rest_controller
from pyfly.web import request_mapping, get_mapping
from my_service.services import GreetingService


@rest_controller
@request_mapping("/api/greetings")
class GreetingController:

    def __init__(self, greeting_service: GreetingService) -> None:
        self._service = greeting_service

    @get_mapping("")
    async def list_greetings(self) -> list[dict]:
        return await self._service.list_greetings()

    @get_mapping("/{name}")
    async def greet(self, name: str) -> dict:
        message = await self._service.get_greeting(name)
        return {"message": message}
```

**How dependency injection works:**

1. PyFly scans `my_service` and finds both `GreetingService` and `GreetingController`.
2. Both are registered in the DI container as singletons.
3. When creating `GreetingController`, the container inspects its `__init__` type
   hints.
4. It sees `greeting_service: GreetingService` and resolves the `GreetingService`
   singleton.
5. The controller is created with the service automatically injected.

No configuration files, no XML, no factories. Just type hints.

---

## Step 8: Add Error Handling

PyFly provides a rich exception hierarchy that automatically maps to HTTP status
codes. Update `src/my_service/services.py`:

```python
from pyfly.container import service
from pyfly.kernel.exceptions import ResourceNotFoundException, ValidationException


@service
class GreetingService:

    _greetings = {
        "world": "Hello, World! Welcome to PyFly.",
        "python": "Hello, Pythonista! PyFly loves Python.",
    }

    async def get_greeting(self, name: str) -> str:
        if not name or not name.strip():
            raise ValidationException(
                "Name must not be empty",
                code="EMPTY_NAME",
            )

        key = name.lower()
        if key in self._greetings:
            return self._greetings[key]
        return f"Hello, {name}! Welcome to PyFly."

    async def get_greeting_strict(self, name: str) -> str:
        """Get a greeting, raising 404 if the name is not known."""
        key = name.lower()
        if key not in self._greetings:
            raise ResourceNotFoundException(
                f"No greeting found for '{name}'",
                code="GREETING_NOT_FOUND",
                context={"name": name},
            )
        return self._greetings[key]

    async def list_greetings(self) -> list[dict]:
        return [
            {"name": name, "message": message}
            for name, message in self._greetings.items()
        ]
```

When these exceptions are thrown, PyFly's global exception handler automatically:

1. Maps the exception to the correct HTTP status code (`422` for `ValidationException`,
   `404` for `ResourceNotFoundException`).
2. Builds a structured JSON error response with the message, code, transaction ID,
   timestamp, and context.
3. Returns the response to the client.

You do not need to write try/except blocks in your controllers. The framework
handles it globally.

**Example error response (HTTP 404):**

```json
{
    "error": {
        "message": "No greeting found for 'unknown'",
        "code": "GREETING_NOT_FOUND",
        "transaction_id": "tx-abc-123",
        "timestamp": "2026-01-15T10:30:00Z",
        "status": 404,
        "path": "/api/greetings/unknown",
        "context": {
            "name": "unknown"
        }
    }
}
```

---

## Step 9: Run the Application

Start the development server:

```bash
pyfly run --reload
```

The `--reload` flag enables automatic restart when you modify source files, which is
ideal for development.

You should see output like:

```
                _____.__
______ ___.__._/ ____\  | ___.__.
\____ <   |  |\   __\|  |<   |  |
|  |_> >___  | |  |  |  |_\___  |
|   __// ____| |__|  |____/ ____|
|__|   \/                 \/

  PyFly v26.05.01 | Python 3.12.0

2026-01-15T10:30:00Z [info] starting_application  app=my-service version=0.1.0
2026-01-15T10:30:00Z [info] no_active_profiles     message=No active profiles set, falling back to default
2026-01-15T10:30:00Z [info] loaded_config          source=pyfly-defaults.yaml (framework defaults)
2026-01-15T10:30:00Z [info] loaded_config          source=pyfly.yaml
2026-01-15T10:30:00Z [info] scanned_package        package=my_service beans_found=2
2026-01-15T10:30:00Z [info] application_started    app=my-service startup_time_s=0.015 beans_initialized=2
2026-01-15T10:30:00Z [info] mapped_endpoints       count=3 routes=...
2026-01-15T10:30:00Z [info] api_documentation      swagger_ui=http://0.0.0.0:8080/docs redoc=http://0.0.0.0:8080/redoc
INFO:     Uvicorn running on http://0.0.0.0:8080
```

Your application is now running with:

- **API endpoints** at `http://localhost:8080/api/greetings`
- **Swagger UI** at `http://localhost:8080/docs`
- **ReDoc** at `http://localhost:8080/redoc`
- **OpenAPI spec** at `http://localhost:8080/openapi.json`

---

## Step 10: Test the API

Open a new terminal and test your endpoints with `curl`:

```bash
# List all greetings
curl http://localhost:8080/api/greetings
# [{"name": "world", "message": "Hello, World! Welcome to PyFly."}, ...]

# Greet by name
curl http://localhost:8080/api/greetings/Alice
# {"message": "Hello, Alice! Welcome to PyFly."}

# Known name with custom message
curl http://localhost:8080/api/greetings/python
# {"message": "Hello, Pythonista! PyFly loves Python."}
```

You can also open `http://localhost:8080/docs` in your browser to explore the API
interactively using Swagger UI.

---

## Step 11: Add a Repository

To demonstrate the full layered architecture, add a repository. Create
`src/my_service/repositories.py`:

```python
from pyfly.container import repository


@repository
class GreetingRepository:
    """In-memory greeting repository (replace with database later)."""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {
            "1": {"id": "1", "name": "world", "message": "Hello, World!"},
            "2": {"id": "2", "name": "python", "message": "Hello, Pythonista!"},
        }
        self._counter = 2

    async def find_all(self) -> list[dict]:
        return list(self._store.values())

    async def find_by_id(self, greeting_id: str) -> dict | None:
        return self._store.get(greeting_id)

    async def find_by_name(self, name: str) -> dict | None:
        for greeting in self._store.values():
            if greeting["name"].lower() == name.lower():
                return greeting
        return None

    async def save(self, greeting: dict) -> dict:
        if "id" not in greeting:
            self._counter += 1
            greeting["id"] = str(self._counter)
        self._store[greeting["id"]] = greeting
        return greeting
```

Now update the service to use the repository:

```python
from pyfly.container import service
from pyfly.kernel.exceptions import ResourceNotFoundException
from my_service.repositories import GreetingRepository


@service
class GreetingService:

    def __init__(self, greeting_repository: GreetingRepository) -> None:
        self._repo = greeting_repository

    async def get_greeting(self, name: str) -> str:
        greeting = await self._repo.find_by_name(name)
        if greeting:
            return greeting["message"]
        return f"Hello, {name}! Welcome to PyFly."

    async def list_greetings(self) -> list[dict]:
        return await self._repo.find_all()

    async def create_greeting(self, name: str, message: str) -> dict:
        return await self._repo.save({"name": name, "message": message})
```

And add a POST endpoint to the controller:

```python
from pydantic import BaseModel
from pyfly.container import rest_controller
from pyfly.web import request_mapping, get_mapping, post_mapping, Body
from my_service.services import GreetingService


class CreateGreetingRequest(BaseModel):
    name: str
    message: str


@rest_controller
@request_mapping("/api/greetings")
class GreetingController:

    def __init__(self, greeting_service: GreetingService) -> None:
        self._service = greeting_service

    @get_mapping("")
    async def list_greetings(self) -> list[dict]:
        return await self._service.list_greetings()

    @get_mapping("/{name}")
    async def greet(self, name: str) -> dict:
        message = await self._service.get_greeting(name)
        return {"message": message}

    @post_mapping("", status_code=201)
    async def create_greeting(self, body: Body[CreateGreetingRequest]) -> dict:
        return await self._service.create_greeting(
            name=body.name,
            message=body.message,
        )
```

The `Body[CreateGreetingRequest]` type annotation tells PyFly to:

1. Read the JSON request body.
2. Validate it against `CreateGreetingRequest` (a Pydantic model).
3. Pass the validated instance to your handler.

If validation fails, a `422 Unprocessable Entity` response is returned automatically.

Test the new endpoint:

```bash
# Create a new greeting
curl -X POST http://localhost:8080/api/greetings \
  -H "Content-Type: application/json" \
  -d '{"name": "rust", "message": "Hello, Rustacean!"}'
# {"id": "3", "name": "rust", "message": "Hello, Rustacean!"}

# Verify it was saved
curl http://localhost:8080/api/greetings
# [... includes the new greeting ...]
```

---

## Step 12: Check Your Environment

PyFly includes a diagnostic command that verifies your development environment:

```bash
pyfly doctor
```

This checks:

- Python version (3.12+ required)
- Virtual environment status
- Required tools and dependencies
- PyFly installation and configuration

If any issues are found, the doctor command provides guidance on how to fix them.

---

## What's Next

Congratulations -- you have built a working PyFly REST API with dependency injection,
a service layer, a repository, request validation, and structured error handling.
Here is where to go next:

### Core Guides

- [Architecture Guide](architecture.md) -- Understand the hexagonal architecture and
  module layers
- [Dependency Injection](modules/dependency-injection.md) -- Constructor injection,
  `@primary`, `@Qualifier`, scopes, and the full DI container API
- [Configuration](modules/configuration.md) -- Profiles, `@config_properties`,
  environment variables, and property binding

### Web Layer

- [Web Guide](modules/web.md) -- Controllers, request parameters (`PathVar`,
  `QueryParam`, `Body`, `Header`, `Cookie`), response handling, CORS, middleware
- [Validation Guide](modules/validation.md) -- `validate_model()`, `@validate_input`,
  `@validator`, and Pydantic integration
- [Error Handling](modules/error-handling.md) -- Full exception hierarchy, HTTP status
  mapping, `ErrorResponse`, and `FieldError`

### Data and Events

- [Data Commons](modules/data.md) -- Repository ports, derived query parsing, pagination, sorting, entity mapping
- [Data Relational](modules/data-relational.md) -- SQLAlchemy adapter: specifications, transactions, custom queries
- [Events](modules/events.md) -- Event-driven architecture, `@event_listener`,
  `InMemoryEventBus`
- [Messaging](modules/messaging.md) -- Kafka and RabbitMQ integration

### Production

- [Actuator](modules/actuator.md) -- Health checks, beans endpoint, environment info
- [Observability](modules/observability.md) -- Metrics, tracing, and structured logging
- [Resilience](modules/resilience.md) -- Circuit breakers, retry policies, bulkheads
- [Testing](modules/testing.md) -- `PyFlyTestCase`, `create_test_container()`, event
  assertions

### Tools

- [CLI Reference](cli.md) -- All `pyfly` CLI commands (`new`, `run`, `doctor`, etc.)
