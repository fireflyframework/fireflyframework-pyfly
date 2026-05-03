# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Jinja2-based project template renderer for scaffolding."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, PackageLoader

# Available features that map to PyFly extras
AVAILABLE_FEATURES: list[str] = [
    "web",
    "fastapi",
    "data-relational",
    "data-document",
    "eda",
    "cache",
    "client",
    "security",
    "scheduling",
    "observability",
    "cqrs",
    "shell",
    "transactional",
    "granian",
    "hypercorn",
]

# Default features per archetype
DEFAULT_FEATURES: dict[str, list[str]] = {
    "core": [],
    "web-api": ["web"],
    "fastapi-api": ["fastapi"],
    "web": ["web"],
    "hexagonal": ["web"],
    "library": [],
    "cli": ["shell"],
}

# Archetype descriptions for interactive mode
ARCHETYPE_DESCRIPTIONS: dict[str, str] = {
    "core": "Minimal microservice",
    "web-api": "REST API with Starlette (layered architecture)",
    "fastapi-api": "REST API with FastAPI (layered architecture)",
    "web": "Server-rendered web application with HTML templates",
    "hexagonal": "Hexagonal architecture (ports & adapters)",
    "library": "Reusable library package",
    "cli": "Command-line application with interactive shell",
}

# Rich metadata for archetypes (wizard display)
ARCHETYPE_DETAILS: dict[str, dict[str, str | list[str]]] = {
    "core": {
        "title": "Core Service",
        "tagline": "Minimal microservice with DI container and config",
        "layers": ["Application", "Config"],
        "good_for": "Background workers, CLI tools, lightweight services",
    },
    "web-api": {
        "title": "REST API — Starlette (Layered)",
        "tagline": "REST API with Starlette, controller/service/repository layers",
        "layers": ["Controllers", "Services", "Models", "Repositories"],
        "good_for": "CRUD APIs, backend services, microservices",
    },
    "fastapi-api": {
        "title": "REST API — FastAPI (Layered)",
        "tagline": "REST API with FastAPI, native OpenAPI, controller/service/repository layers",
        "layers": ["Controllers", "Services", "Models", "Repositories"],
        "good_for": "CRUD APIs with native OpenAPI, backend services, microservices",
    },
    "web": {
        "title": "Web Application (SSR)",
        "tagline": "Server-rendered HTML with Jinja2 templates and static assets",
        "layers": ["Controllers", "Services", "Templates", "Static"],
        "good_for": "Websites, dashboards, admin panels, multi-page applications",
    },
    "hexagonal": {
        "title": "Hexagonal (Ports & Adapters)",
        "tagline": "Clean architecture with domain isolation",
        "layers": ["Domain", "Application", "Infrastructure", "API"],
        "good_for": "Complex domains, DDD, long-lived systems",
    },
    "library": {
        "title": "Library Package",
        "tagline": "Reusable library with py.typed and packaging best practices",
        "layers": ["Package"],
        "good_for": "Shared utilities, SDKs, internal libraries",
    },
    "cli": {
        "title": "CLI Application",
        "tagline": "Command-line application with interactive shell and DI",
        "layers": ["Commands", "Services", "Config"],
        "good_for": "DevOps tools, admin utilities, batch processors, interactive CLIs",
    },
}

# Feature groups for categorized display in the wizard
FEATURE_GROUPS: list[tuple[str, list[str]]] = [
    ("Web & API", ["web", "fastapi"]),
    ("Data & Storage", ["data-relational", "data-document", "cache"]),
    ("Messaging & Events", ["eda", "cqrs"]),
    ("Distributed Transactions", ["transactional"]),
    ("Infrastructure", ["client", "security", "scheduling", "observability"]),
    ("Servers", ["granian", "hypercorn"]),
    ("CLI & Shell", ["shell"]),
]

# Extended feature details (what gets added)
FEATURE_DETAILS: dict[str, dict[str, str]] = {
    "web": {
        "short": "HTTP server, REST controllers, OpenAPI docs",
        "adds": "Starlette server, @rest_controller, Swagger UI, ReDoc, WebFilters",
    },
    "fastapi": {
        "short": "FastAPI (REST API with native OpenAPI)",
        "adds": "FastAPI web adapter, native OpenAPI, automatic request validation",
    },
    "data-relational": {
        "short": "Data Relational — SQL databases (SQLAlchemy ORM)",
        "adds": "Repository[T, ID], BaseEntity, Alembic, SQLite default, RepositoryPort",
    },
    "data-document": {
        "short": "Data Document — Document databases (Beanie ODM)",
        "adds": "MongoRepository[T, ID], BaseDocument, Beanie, Motor async driver, RepositoryPort",
    },
    "eda": {
        "short": "Event-driven architecture, in-memory event bus",
        "adds": "EventPublisher, @event_listener, @publish_result, ErrorStrategy",
    },
    "cache": {
        "short": "Caching with in-memory adapter",
        "adds": "@cacheable, @cache_evict, @cache_put, Redis optional",
    },
    "client": {
        "short": "Resilient HTTP client with retry and circuit breaker",
        "adds": "@http_client, CircuitBreaker, RetryPolicy, @get, @post",
    },
    "security": {
        "short": "JWT authentication, password hashing",
        "adds": "JWTService, BcryptPasswordEncoder, SecurityMiddleware, @secure",
    },
    "scheduling": {
        "short": "Cron-based task scheduling",
        "adds": "@scheduled, CronExpression, TaskScheduler, @async_method",
    },
    "observability": {
        "short": "Prometheus metrics, OpenTelemetry tracing",
        "adds": "@timed, @counted, @span, MetricsRegistry, HealthAggregator",
    },
    "cqrs": {
        "short": "Command/Query Responsibility Segregation",
        "adds": "CommandBus, QueryBus, CommandHandler, QueryHandler, HandlerRegistry",
    },
    "shell": {
        "short": "Spring Shell-inspired CLI commands with DI",
        "adds": "@shell_component, @shell_method, CommandLineRunner, ClickShellAdapter",
    },
    "transactional": {
        "short": "Distributed SAGA and TCC transaction patterns",
        "adds": "@saga, @saga_step, @tcc, @tcc_participant, SagaEngine, TccEngine",
    },
    "granian": {
        "short": "Granian (Rust ASGI server, ~3x faster)",
        "adds": "Granian ASGI server (Rust/tokio, highest performance)",
    },
    "hypercorn": {
        "short": "Hypercorn (HTTP/2 & HTTP/3 server)",
        "adds": "Hypercorn ASGI server (HTTP/2, HTTP/3 support)",
    },
}

# Post-generation tips per feature
FEATURE_TIPS: dict[str, list[str]] = {
    "web": [
        "Visit http://localhost:8080/docs for Swagger UI",
        "Visit http://localhost:8080/redoc for ReDoc",
        "Visit http://localhost:8080/admin for the Admin Dashboard",
    ],
    "data-relational": [
        "Run 'pyfly db init' to set up Alembic migrations",
        "SQLite is configured by default (zero infrastructure)",
    ],
    "data-document": [
        "Configure MongoDB URI in pyfly.yaml under pyfly.data.document.uri",
        "Documents use Beanie ODM (Pydantic models + Motor async driver)",
    ],
    "eda": [
        "Events use in-memory bus by default — switch to Kafka for production",
    ],
    "cache": [
        "Cache uses in-memory adapter — switch to Redis for production",
    ],
    "client": [
        "Configure base URLs in pyfly.yaml under pyfly.client.*",
    ],
    "security": [
        "Change JWT secret in pyfly.yaml before deploying!",
    ],
    "scheduling": [
        "Use @scheduled(cron='*/5 * * * *') for cron-based tasks",
    ],
    "observability": [
        "Metrics endpoint: GET /actuator/prometheus (when actuator enabled)",
    ],
    "cqrs": [
        "Register handlers via @command_handler/@query_handler — HandlerRegistry auto-discovers them",
    ],
    "shell": [
        "Add commands with @shell_method in any @shell_component class",
        "Shell auto-configuration activates when pyfly.shell.enabled=true",
    ],
    "transactional": [
        "Define sagas with @saga and @saga_step — steps run as a DAG with compensation",
        "Define TCC transactions with @tcc and @tcc_participant (Try-Confirm-Cancel)",
        "Enable via pyfly.transactional.enabled=true in pyfly.yaml",
    ],
    "fastapi": [
        "Visit http://localhost:8080/docs for FastAPI's native Swagger UI",
        "Visit http://localhost:8080/redoc for ReDoc",
    ],
    "granian": [
        "Granian is auto-selected when installed — configure via pyfly.server.granian.*",
    ],
    "hypercorn": [
        "Hypercorn is auto-selected when installed — configure via pyfly.server.hypercorn.*",
    ],
}

# Template → output path mapping per archetype.
# Output paths use {package_name} as a placeholder.
_ARCHETYPE_FILES: dict[str, list[tuple[str, str]]] = {
    "core": [
        ("pyproject.toml.j2", "pyproject.toml"),
        ("app.py.j2", "src/{package_name}/app.py"),
        ("main.py.j2", "src/{package_name}/main.py"),
        ("init.py.j2", "src/{package_name}/__init__.py"),
        ("pyfly.yaml.j2", "pyfly.yaml"),
        ("conftest.py.j2", "tests/conftest.py"),
        ("init_empty.j2", "tests/__init__.py"),
        ("gitignore.j2", ".gitignore"),
        ("readme.md.j2", "README.md"),
        ("dockerfile.j2", "Dockerfile"),
        ("env.example.j2", ".env.example"),
    ],
    "web-api": [
        ("pyproject.toml.j2", "pyproject.toml"),
        ("app.py.j2", "src/{package_name}/app.py"),
        ("main.py.j2", "src/{package_name}/main.py"),
        ("init.py.j2", "src/{package_name}/__init__.py"),
        ("pyfly.yaml.j2", "pyfly.yaml"),
        ("conftest.py.j2", "tests/conftest.py"),
        ("init_empty.j2", "tests/__init__.py"),
        ("gitignore.j2", ".gitignore"),
        ("readme.md.j2", "README.md"),
        ("dockerfile.j2", "Dockerfile"),
        ("env.example.j2", ".env.example"),
        # Controllers
        ("init_empty.j2", "src/{package_name}/controllers/__init__.py"),
        ("health_controller.py.j2", "src/{package_name}/controllers/health_controller.py"),
        ("todo_controller.py.j2", "src/{package_name}/controllers/todo_controller.py"),
        # Services
        ("init_empty.j2", "src/{package_name}/services/__init__.py"),
        ("todo_service.py.j2", "src/{package_name}/services/todo_service.py"),
        # Models
        ("init_empty.j2", "src/{package_name}/models/__init__.py"),
        ("todo_model.py.j2", "src/{package_name}/models/todo.py"),
        # Repositories
        ("init_empty.j2", "src/{package_name}/repositories/__init__.py"),
        ("todo_repository.py.j2", "src/{package_name}/repositories/todo_repository.py"),
        # Tests
        ("test_todo_service.py.j2", "tests/test_todo_service.py"),
    ],
    "fastapi-api": [
        ("pyproject.toml.j2", "pyproject.toml"),
        ("app.py.j2", "src/{package_name}/app.py"),
        ("main.py.j2", "src/{package_name}/main.py"),
        ("init.py.j2", "src/{package_name}/__init__.py"),
        ("pyfly.yaml.j2", "pyfly.yaml"),
        ("conftest.py.j2", "tests/conftest.py"),
        ("init_empty.j2", "tests/__init__.py"),
        ("gitignore.j2", ".gitignore"),
        ("readme.md.j2", "README.md"),
        ("dockerfile.j2", "Dockerfile"),
        ("env.example.j2", ".env.example"),
        # Controllers
        ("init_empty.j2", "src/{package_name}/controllers/__init__.py"),
        ("health_controller.py.j2", "src/{package_name}/controllers/health_controller.py"),
        ("todo_controller.py.j2", "src/{package_name}/controllers/todo_controller.py"),
        # Services
        ("init_empty.j2", "src/{package_name}/services/__init__.py"),
        ("todo_service.py.j2", "src/{package_name}/services/todo_service.py"),
        # Models
        ("init_empty.j2", "src/{package_name}/models/__init__.py"),
        ("todo_model.py.j2", "src/{package_name}/models/todo.py"),
        # Repositories
        ("init_empty.j2", "src/{package_name}/repositories/__init__.py"),
        ("todo_repository.py.j2", "src/{package_name}/repositories/todo_repository.py"),
        # Tests
        ("test_todo_service.py.j2", "tests/test_todo_service.py"),
    ],
    "hexagonal": [
        ("pyproject.toml.j2", "pyproject.toml"),
        ("app.py.j2", "src/{package_name}/app.py"),
        ("main.py.j2", "src/{package_name}/main.py"),
        ("init.py.j2", "src/{package_name}/__init__.py"),
        ("pyfly.yaml.j2", "pyfly.yaml"),
        ("conftest.py.j2", "tests/conftest.py"),
        ("init_empty.j2", "tests/__init__.py"),
        ("gitignore.j2", ".gitignore"),
        ("readme.md.j2", "README.md"),
        ("dockerfile.j2", "Dockerfile"),
        ("env.example.j2", ".env.example"),
        # Domain layer
        ("init_empty.j2", "src/{package_name}/domain/__init__.py"),
        ("hex/domain_models.py.j2", "src/{package_name}/domain/models.py"),
        ("hex/domain_events.py.j2", "src/{package_name}/domain/events.py"),
        ("init_empty.j2", "src/{package_name}/domain/ports/__init__.py"),
        ("hex/ports_inbound.py.j2", "src/{package_name}/domain/ports/inbound.py"),
        ("hex/ports_outbound.py.j2", "src/{package_name}/domain/ports/outbound.py"),
        # Application layer
        ("init_empty.j2", "src/{package_name}/application/__init__.py"),
        ("hex/app_services.py.j2", "src/{package_name}/application/services.py"),
        # Infrastructure layer
        ("init_empty.j2", "src/{package_name}/infrastructure/__init__.py"),
        ("init_empty.j2", "src/{package_name}/infrastructure/adapters/__init__.py"),
        ("hex/persistence.py.j2", "src/{package_name}/infrastructure/adapters/persistence.py"),
        ("hex/config.py.j2", "src/{package_name}/infrastructure/config.py"),
        # API layer
        ("init_empty.j2", "src/{package_name}/api/__init__.py"),
        ("hex/controllers.py.j2", "src/{package_name}/api/controllers.py"),
        ("hex/dto.py.j2", "src/{package_name}/api/dto.py"),
        # Tests
        ("init_empty.j2", "tests/domain/__init__.py"),
        ("hex/test_models.py.j2", "tests/domain/test_models.py"),
        ("init_empty.j2", "tests/application/__init__.py"),
        ("hex/test_services.py.j2", "tests/application/test_services.py"),
    ],
    "web": [
        # Shared
        ("pyproject.toml.j2", "pyproject.toml"),
        ("app.py.j2", "src/{package_name}/app.py"),
        ("web/main.py.j2", "src/{package_name}/main.py"),
        ("init.py.j2", "src/{package_name}/__init__.py"),
        ("pyfly.yaml.j2", "pyfly.yaml"),
        ("conftest.py.j2", "tests/conftest.py"),
        ("init_empty.j2", "tests/__init__.py"),
        ("gitignore.j2", ".gitignore"),
        ("readme.md.j2", "README.md"),
        ("dockerfile.j2", "Dockerfile"),
        ("env.example.j2", ".env.example"),
        # Controllers
        ("init_empty.j2", "src/{package_name}/controllers/__init__.py"),
        ("health_controller.py.j2", "src/{package_name}/controllers/health_controller.py"),
        ("web/home_controller.py.j2", "src/{package_name}/controllers/home_controller.py"),
        # Services
        ("init_empty.j2", "src/{package_name}/services/__init__.py"),
        ("web/page_service.py.j2", "src/{package_name}/services/page_service.py"),
        # Templates (runtime Jinja2 HTML)
        ("web/base.html.j2", "src/{package_name}/templates/base.html"),
        ("web/home.html.j2", "src/{package_name}/templates/home.html"),
        ("web/about.html.j2", "src/{package_name}/templates/about.html"),
        # Static assets
        ("web/style.css.j2", "src/{package_name}/static/css/style.css"),
        # Tests
        ("web/test_home_controller.py.j2", "tests/test_home_controller.py"),
    ],
    "library": [
        ("pyproject.toml.j2", "pyproject.toml"),
        ("init.py.j2", "src/{package_name}/__init__.py"),
        ("init_empty.j2", "tests/__init__.py"),
        ("conftest.py.j2", "tests/conftest.py"),
        ("gitignore.j2", ".gitignore"),
        ("readme.md.j2", "README.md"),
        ("py.typed.j2", "src/{package_name}/py.typed"),
    ],
    "cli": [
        ("pyproject.toml.j2", "pyproject.toml"),
        ("app.py.j2", "src/{package_name}/app.py"),
        ("cli/main.py.j2", "src/{package_name}/main.py"),
        ("init.py.j2", "src/{package_name}/__init__.py"),
        ("pyfly.yaml.j2", "pyfly.yaml"),
        ("conftest.py.j2", "tests/conftest.py"),
        ("init_empty.j2", "tests/__init__.py"),
        ("gitignore.j2", ".gitignore"),
        ("readme.md.j2", "README.md"),
        ("dockerfile.j2", "Dockerfile"),
        ("env.example.j2", ".env.example"),
        # Commands
        ("init_empty.j2", "src/{package_name}/commands/__init__.py"),
        ("cli/hello_command.py.j2", "src/{package_name}/commands/hello_command.py"),
        # Services
        ("init_empty.j2", "src/{package_name}/services/__init__.py"),
        ("cli/greeting_service.py.j2", "src/{package_name}/services/greeting_service.py"),
        # Tests
        ("cli/test_hello_command.py.j2", "tests/test_hello_command.py"),
    ],
}


def _to_package_name(name: str) -> str:
    """Convert project name to valid Python package name."""
    return name.replace("-", "_").replace(" ", "_").lower()


def _write(path: Path, content: str) -> None:
    """Write content to a file, creating parent directories if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _build_context(name: str, archetype: str, features: list[str]) -> dict[str, object]:
    """Build the Jinja2 template context."""
    package_name = _to_package_name(name)
    return {
        "name": name,
        "package_name": package_name,
        "archetype": archetype,
        "features": features,
        "has_web": "web" in features,
        "has_data": "data-relational" in features,
        "has_mongodb": "data-document" in features,
        "has_eda": "eda" in features,
        "has_cache": "cache" in features,
        "has_client": "client" in features,
        "has_security": "security" in features,
        "has_scheduling": "scheduling" in features,
        "has_observability": "observability" in features,
        "has_cqrs": "cqrs" in features,
        "has_shell": "shell" in features,
        "has_transactional": "transactional" in features,
        "has_fastapi": "fastapi" in features,
        "has_granian": "granian" in features,
        "has_hypercorn": "hypercorn" in features,
    }


def _get_env() -> Environment:
    """Create the Jinja2 template environment."""
    return Environment(
        loader=PackageLoader("pyfly.cli", "templates"),
        keep_trailing_newline=True,
        lstrip_blocks=True,
        trim_blocks=True,
    )


def generate_project(name: str, project_dir: Path, archetype: str, features: list[str]) -> None:
    """Generate a project from Jinja2 templates.

    Args:
        name: Project name (e.g. ``"my-service"``).
        project_dir: Target directory to create.
        archetype: One of ``core``, ``web-api``, ``fastapi-api``, ``web``, ``hexagonal``, ``library``, ``cli``.
        features: Selected PyFly extras (e.g. ``["web", "data-relational"]``).
    """
    env = _get_env()
    context = _build_context(name, archetype, features)
    package_name = _to_package_name(name)

    for template_name, output_path in _ARCHETYPE_FILES[archetype]:
        output_path = output_path.replace("{package_name}", package_name)
        rendered = env.get_template(template_name).render(context)
        _write(project_dir / output_path, rendered)
