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
"""Application bootstrap — the entry point for PyFly applications."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from pyfly.container.exceptions import BeanCreationException
from pyfly.container.scanner import scan_package
from pyfly.core.banner import BannerPrinter
from pyfly.core.config import Config

try:
    from pyfly.logging.structlog_adapter import StructlogAdapter as _DefaultLoggingAdapter
except ImportError:
    from pyfly.logging.stdlib_adapter import StdlibLoggingAdapter as _DefaultLoggingAdapter  # type: ignore[assignment]

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext

T = TypeVar("T")


def pyfly_application(
    name: str,
    version: str = "0.1.0",
    scan_packages: list[str] | None = None,
    description: str = "",
) -> Any:
    """Decorator marking a class as a PyFly application entry point."""

    def decorator(cls: type[T]) -> type[T]:
        cls.__pyfly_app_name__ = name  # type: ignore[attr-defined]
        cls.__pyfly_app_version__ = version  # type: ignore[attr-defined]
        cls.__pyfly_scan_packages__ = scan_packages or []  # type: ignore[attr-defined]
        cls.__pyfly_app_description__ = description  # type: ignore[attr-defined]
        return cls

    return decorator


def _wiring_summary_fields(wiring: dict[str, int]) -> dict[str, int]:
    """Build the fields shown in the startup ``wiring_summary`` log line.

    Surfaces every decorator-wiring count — including EDA ``@event_listener``
    subscriptions (``event_listeners_eda``), which were previously omitted so the
    summary under-reported wired EDA listeners as absent.
    """
    return {
        "event_listeners": wiring.get("event_listeners", 0),
        "event_listeners_eda": wiring.get("event_listeners_eda", 0),
        "message_listeners": wiring.get("message_listeners", 0),
        "cqrs_handlers": wiring.get("cqrs_handlers", 0),
        "scheduled_tasks": wiring.get("scheduled", 0),
        "async_methods": wiring.get("async_methods", 0),
        "post_processors": wiring.get("post_processors", 0),
    }


class PyFlyApplication:
    """Main application class that bootstraps the framework.

    Startup sequence (Spring Boot parity):
    1. Configure logging (from pyfly.yaml logging section)
    2. Print banner (respecting pyfly.banner.mode)
    3. Log "Starting {app} v{version}"
    4. Log "Active profiles: {profiles}" or "No active profiles set"
    5. Load profile-specific config files
    6. Filter beans by active profiles
    7. Sort beans by @order value
    8. Initialize beans (respecting order)
    9. Log "Started {app} in {time}s ({count} beans initialized)"
    """

    def __init__(self, app_class: type, config_path: str | Path | None = None) -> None:
        self._app_class = app_class
        self._name: str = getattr(app_class, "__pyfly_app_name__", "pyfly-app")
        self._version: str = getattr(app_class, "__pyfly_app_version__", "0.1.0")
        self._description: str = getattr(app_class, "__pyfly_app_description__", "")
        self._scan_packages: list[str] = getattr(app_class, "__pyfly_scan_packages__", [])
        self._startup_time: float = 0.0
        # Web runtime info, populated by the ASGI lifespan in a generated main.py.
        # Declared here (typed) so a scaffolded entry point that assigns them stays
        # ``mypy --strict`` clean; empty defaults fall back to config when unset.
        self._route_metadata: list[Any] = []
        self._docs_enabled: bool = False
        self._host: str = ""
        self._port: int = 0

        # 1a. Compute starter property defaults from any @enable_*_stack
        #     decorators on the application class. These flow into the
        #     config loader as a layer between framework defaults and the
        #     user's pyfly.yaml — so the bundle activates the modules it
        #     promises while explicit user values still win.
        starter_defaults = self._collect_starter_properties(app_class)

        # 1b. Load configuration (with profile merging, multi-source)
        config_dir = self._find_config_dir(config_path)
        active_profiles = self._resolve_profiles_early(config_dir)
        if config_dir:
            self.config = Config.from_sources(
                config_dir,
                active_profiles=active_profiles,
                starter_defaults=starter_defaults,
            )
        else:
            base = Config._load_framework_defaults()
            if starter_defaults:
                base = Config._deep_merge(base, starter_defaults)
            self.config = Config(base)
            sources = ["pyfly-defaults.yaml (framework defaults)"]
            if starter_defaults:
                sources.append("starter defaults (@enable_*_stack)")
            self.config._loaded_sources = sources

        # 1c. Import remote configuration from a config server, if one is
        #     configured (Spring Cloud Config parity). Runs before logging /
        #     context so remote values participate in everything downstream.
        self._import_remote_config(active_profiles)

        # 2. Configure logging
        self._logging = _DefaultLoggingAdapter()
        self._logging.configure(self.config)
        self._logger = self._logging.get_logger("pyfly.core")

        # Deferred import to avoid circular import
        from pyfly.context.application_context import ApplicationContext

        # Create ApplicationContext
        self._context = ApplicationContext(self.config)

        # Auto-discover beans from scanned packages (logging deferred to startup)
        self._scan_results: list[tuple[str, int]] = []
        for package in self._scan_packages:
            try:
                count = scan_package(package, self._context.container)
                self._scan_results.append((package, count))
            except ImportError as e:
                self._logger.warning("scan_failed", package=package, error=str(e))

    @property
    def context(self) -> ApplicationContext:
        """The ApplicationContext."""
        return self._context

    @property
    def startup_time_seconds(self) -> float:
        """Time taken to start the application, in seconds."""
        return self._startup_time

    async def startup(self) -> None:
        """Start the application with full Spring Boot-style startup sequence."""
        import os
        import platform

        start = time.perf_counter()

        # Determine if startup logs should be suppressed (multi-worker or CLI-printed banner)
        banner_printed = os.environ.get("_PYFLY_BANNER_PRINTED") == "1"
        worker_count = int(os.environ.get("_PYFLY_WORKERS", "1"))
        suppress_logs = banner_printed and worker_count > 1

        # Print banner (skip if already printed by CLI process)
        if not banner_printed:
            profiles = self._context.environment.active_profiles
            banner = BannerPrinter.from_config(
                self.config,
                version=self._version,
                app_name=self._name,
                app_version=self._version,
                active_profiles=profiles,
            )
            banner_text = banner.render()
            if banner_text:
                print(banner_text)  # noqa: T201
                sys.stdout.flush()

        if not suppress_logs:
            # "Starting X using Python 3.13.9 with PID 12345" (mirrors Spring Boot)
            py_version = platform.python_version()
            self._logger.info(
                "starting_application",
                app=self._name,
                version=self._version,
                python=py_version,
                pid=os.getpid(),
            )

            # Runtime environment info
            self._logger.info(
                "runtime_environment",
                os=platform.system(),
                os_version=platform.release(),
                arch=platform.machine(),
                cpus=os.cpu_count() or 1,
                python_impl=platform.python_implementation(),
            )

            profiles = self._context.environment.active_profiles
            if profiles:
                self._logger.info("active_profiles", profiles=profiles)
            else:
                self._logger.info("no_active_profiles", message="No active profiles set, falling back to default")

            # Log loaded config sources
            for source in self.config.loaded_sources:
                self._logger.info("loaded_config", source=source)

            # Log deferred scan results (now appears after banner)
            for package, count in self._scan_results:
                self._logger.info("scanned_package", package=package, beans_found=count)

        # Start the context (handles profile filtering, @order sorting, bean init)
        try:
            await self._context.start()
        except BeanCreationException as exc:
            self._logger.error(
                "application_failed",
                app=self._name,
                error=str(exc),
                subsystem=exc.subsystem,
                provider=exc.provider,
            )
            raise

        self._startup_time = time.perf_counter() - start

        if not suppress_logs:
            # Log comprehensive startup summary (beans, wiring)
            self._log_startup_summary()

            # Log routes and API documentation URLs
            self._log_routes_and_docs()

            # Log server info — like Spring Boot's "Netty started on port 8080"
            self._log_server_info()

            # Final "Started X in Y seconds" — LAST line (Spring Boot parity)
            self._log_started()

    def _log_startup_summary(self) -> None:
        """Log bean and wiring summary."""
        # Bean type breakdown
        stereotype_counts = self._context.get_bean_counts_by_stereotype()
        self._logger.info(
            "bean_summary",
            total=self._context.bean_count,
            services=stereotype_counts.get("service", 0),
            repositories=stereotype_counts.get("repository", 0),
            controllers=stereotype_counts.get("controller", 0) + stereotype_counts.get("rest_controller", 0),
            configurations=stereotype_counts.get("configuration", 0),
        )

        # Decorator wiring counts
        wiring = self._context.wiring_counts
        if any(wiring.values()):
            self._logger.info("wiring_summary", **_wiring_summary_fields(wiring))

    def _log_server_info(self) -> None:
        """Log server configuration — like Spring Boot's 'Netty started on port 8080'."""
        import os

        server_type = os.environ.get("_PYFLY_SERVER_TYPE", "unknown")
        host = os.environ.get("_PYFLY_SERVER_HOST", "0.0.0.0")
        port = os.environ.get("_PYFLY_SERVER_PORT", "8080")
        workers = os.environ.get("_PYFLY_WORKERS", "1")
        event_loop = os.environ.get("_PYFLY_EVENT_LOOP", "auto")
        http = os.environ.get("_PYFLY_HTTP", "auto")

        # Resolve server version at runtime
        server_version = self._resolve_server_version(server_type)

        self._logger.info(
            "server_started",
            server=server_type,
            server_version=server_version,
            host=host,
            port=int(port),
            workers=int(workers),
            event_loop=event_loop,
            http=http,
        )

    def _log_started(self) -> None:
        """Log final 'Started X in Y seconds' — the LAST startup line (Spring Boot parity)."""
        self._logger.info(
            "application_started",
            app=self._name,
            startup_time_s=round(self._startup_time, 3),
            beans_initialized=self._context.bean_count,
        )

    @staticmethod
    def _resolve_server_version(server_type: str) -> str:
        """Resolve the installed version of the server library."""
        try:
            from importlib.metadata import version

            name_map = {"granian": "granian", "uvicorn": "uvicorn", "hypercorn": "hypercorn"}
            pkg = name_map.get(server_type)
            if pkg:
                return version(pkg)
        except Exception:
            pass
        return "unknown"

    def _log_routes_and_docs(self) -> None:
        """Log mapped endpoints and documentation URLs (Spring Boot style)."""
        route_metadata = self._route_metadata
        docs_enabled = self._docs_enabled
        from pyfly.config.properties.server import resolve_app_host, resolve_app_port

        host = self._host or resolve_app_host(self.config)
        port = self._port or resolve_app_port(self.config)

        if route_metadata:
            self._logger.info("mapped_endpoints", count=len(route_metadata))
            for rm in route_metadata:
                method = rm.http_method.upper()
                handler_name = rm.handler_name
                controller_name = ""
                if hasattr(rm, "handler") and hasattr(rm.handler, "__self__"):
                    controller_name = type(rm.handler.__self__).__name__ + "."
                self._logger.info(
                    "request_mapping",
                    method=method,
                    path=rm.path,
                    handler=f"{controller_name}{handler_name}",
                )

        if docs_enabled:
            base_url = f"http://{host}:{port}"
            self._logger.info("api_documentation", swagger_ui=f"{base_url}/docs")
            self._logger.info("api_documentation", redoc=f"{base_url}/redoc")
            self._logger.info("api_documentation", openapi=f"{base_url}/openapi.json")

        # Log admin dashboard URL when enabled
        admin_enabled = str(self.config.get("pyfly.admin.enabled", "false")).lower() in ("true", "1", "yes")
        if admin_enabled:
            base_url = f"http://{host}:{port}"
            admin_path = str(self.config.get("pyfly.admin.path", "/admin"))
            self._logger.info("admin_dashboard", url=f"{base_url}{admin_path}")

    async def run(self, args: list[str] | None = None) -> int:
        """Start the app, dispatch the shell, then shut down (CLI entry point).

        Resolves the :class:`ShellRunnerPort` (populated with ``@shell_command``
        handlers during startup) and runs the given args — or ``sys.argv[1:]`` —
        falling back to an interactive REPL when no command is supplied. This is
        what the generated ``cli`` archetype's ``main()`` awaits (audit #1).
        """
        from pyfly.shell.ports.outbound import ShellRunnerPort

        await self.startup()
        try:
            runner = self._context.get_bean(ShellRunnerPort)  # type: ignore[type-abstract]
            argv = sys.argv[1:] if args is None else args
            if argv:
                return await runner.run(argv)
            await runner.run_interactive()
            return 0
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Shutdown the application — stop the ApplicationContext."""
        self._logger.info("shutting_down", app=self._name)
        await self._context.stop()

    @staticmethod
    def _collect_starter_properties(app_class: type) -> dict[str, Any]:
        """Build the nested starter-defaults dict from ``@enable_*_stack`` decorators.

        Returns a nested mapping ready to be deep-merged with
        :code:`Config._load_framework_defaults()` and the user's
        ``pyfly.yaml``. Empty dict if no starter decorators are present.
        """
        defaults: dict[str, Any] = {}
        for attr_name in dir(app_class):
            if not attr_name.startswith("__pyfly_starter_") or not attr_name.endswith("__"):
                continue
            stack_props = getattr(app_class, attr_name, None)
            if not isinstance(stack_props, dict):
                continue
            for dotted_key, raw_value in stack_props.items():
                PyFlyApplication._merge_dotted(defaults, dotted_key, raw_value)
        return defaults

    @staticmethod
    def _merge_dotted(target: dict[str, Any], key: str, value: Any) -> None:
        """Insert *value* into *target* under the dotted *key*."""
        parts = key.split(".")
        cursor = target
        for part in parts[:-1]:
            existing = cursor.get(part)
            if not isinstance(existing, dict):
                existing = {}
                cursor[part] = existing
            cursor = existing
        cursor[parts[-1]] = value

    def _import_remote_config(self, active_profiles: list[str]) -> None:
        """Fetch config from a remote config server and merge it as a
        high-precedence source (audit #84).

        Active when ``pyfly.cloud.config.uri`` (or ``pyfly.config.import``) is set
        and ``pyfly.cloud.config.enabled`` is not false. Non-fatal by default —
        an unreachable server or missing httpx logs a warning and falls back to
        local config — unless ``pyfly.cloud.config.fail-fast`` is true. Runs in
        ``__init__`` before the logging adapter exists, so it uses a stdlib logger.
        """
        import asyncio
        import logging

        uri = self.config.get("pyfly.cloud.config.uri") or self.config.get("pyfly.config.import")
        if not uri:
            return
        if str(self.config.get("pyfly.cloud.config.enabled", "true")).lower() not in ("true", "1", "yes"):
            return

        log = logging.getLogger("pyfly.config")

        # __init__ is synchronous; if an event loop is already running we cannot
        # asyncio.run() — skip rather than deadlock.
        try:
            asyncio.get_running_loop()
            log.warning("Skipping remote config import: an event loop is already running.")
            return
        except RuntimeError:
            pass

        fail_fast = str(self.config.get("pyfly.cloud.config.fail-fast", "false")).lower() in ("true", "1", "yes")
        try:
            from pyfly.config_server.client import ConfigClient

            client = ConfigClient(
                url=str(uri),
                application=str(self.config.get("pyfly.app.name", self._name)),
                profile=",".join(active_profiles) if active_profiles else "default",
                label=str(self.config.get("pyfly.cloud.config.label", "main")),
                username=self.config.get("pyfly.cloud.config.username"),
                password=self.config.get("pyfly.cloud.config.password"),
            )
            remote = asyncio.run(client.fetch())
        except Exception as exc:  # noqa: BLE001 — must not crash startup unless fail-fast
            if fail_fast:
                raise
            log.warning("Remote config import from %s failed (%s); using local config only.", uri, exc)
            return

        if not remote:
            return
        nested: dict[str, Any] = {}
        for dotted_key, value in remote.items():
            self._merge_dotted(nested, dotted_key, value)
        self.config._data = Config._deep_merge(self.config._data, nested)
        self.config._loaded_sources.append(f"config-server ({uri})")

    def _find_config_dir(self, config_path: str | Path | None) -> Path | None:
        """Find the project directory containing config files."""
        if config_path:
            p = Path(config_path)
            return p.parent if p.is_file() else p
        for candidate in ["pyfly.yaml", "pyfly.toml", "config/pyfly.yaml", "config/pyfly.toml"]:
            if Path(candidate).exists():
                return Path(".")
        return None

    @staticmethod
    def _resolve_profiles_early(config_dir: Path | None) -> list[str]:
        """Resolve active profiles before full config load."""
        import os

        import yaml  # type: ignore[import-untyped]

        env_profiles = os.environ.get("PYFLY_PROFILES_ACTIVE", "")
        if env_profiles:
            return [p.strip() for p in env_profiles.split(",") if p.strip()]

        if config_dir is None:
            return []

        for candidate in [config_dir / "config" / "pyfly.yaml", config_dir / "pyfly.yaml"]:
            if candidate.exists():
                with open(candidate) as f:
                    data = yaml.safe_load(f) or {}
                profiles_value = (data.get("pyfly", {}) or {}).get("profiles", {})
                active = profiles_value.get("active", "") if isinstance(profiles_value, dict) else ""
                if active:
                    return [p.strip() for p in str(active).split(",") if p.strip()]

        return []
