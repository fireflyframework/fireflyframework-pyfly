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
"""Integration tests for the server abstraction layer.

Verifies that all available server and event loop adapters satisfy their
respective port protocols, produce correct metadata, and that the
auto-configuration layer is properly wired.
"""

from __future__ import annotations

from importlib import import_module

import pytest

from pyfly.config.properties.server import ServerProperties
from pyfly.server.ports.event_loop import EventLoopPort
from pyfly.server.ports.outbound import ApplicationServerPort
from pyfly.server.types import ServerInfo

_SERVER_ADAPTERS: list[tuple[str, str, str]] = [
    ("pyfly.server.adapters.granian.adapter", "GranianServerAdapter", "granian"),
    ("pyfly.server.adapters.uvicorn.adapter", "UvicornServerAdapter", "uvicorn"),
    ("pyfly.server.adapters.hypercorn.adapter", "HypercornServerAdapter", "hypercorn"),
]

_EVENT_LOOP_ADAPTERS: list[tuple[str, str, str]] = [
    ("pyfly.server.adapters.event_loop.uvloop_adapter", "UvloopEventLoopAdapter", "uvloop"),
    ("pyfly.server.adapters.event_loop.winloop_adapter", "WinloopEventLoopAdapter", "winloop"),
    ("pyfly.server.adapters.event_loop.asyncio_adapter", "AsyncioEventLoopAdapter", "asyncio"),
]


def _available_server_adapters() -> list[tuple[str, object]]:
    """Return (name, instance) pairs for every importable server adapter."""
    adapters: list[tuple[str, object]] = []
    for module_path, class_name, _name in _SERVER_ADAPTERS:
        try:
            mod = import_module(module_path)
            cls = getattr(mod, class_name)
            adapters.append((class_name, cls()))
        except ImportError:
            continue
    return adapters


def _available_event_loop_adapters() -> list[tuple[str, object]]:
    """Return (name, instance) pairs for every importable event loop adapter."""
    adapters: list[tuple[str, object]] = []
    for module_path, class_name, _name in _EVENT_LOOP_ADAPTERS:
        try:
            mod = import_module(module_path)
            cls = getattr(mod, class_name)
            adapters.append((class_name, cls()))
        except ImportError:
            continue
    return adapters


class TestServerResolution:
    """Test that server adapters can be resolved and configured."""

    def test_auto_detect_finds_at_least_one_server(self):
        """At least one ASGI server should be available in the dev environment."""
        adapters = _available_server_adapters()
        assert len(adapters) > 0, "No ASGI server adapter found in dev environment"

    def test_all_available_adapters_implement_protocol(self):
        """Every importable server adapter must satisfy ApplicationServerPort."""
        for class_name, instance in _available_server_adapters():
            assert isinstance(instance, ApplicationServerPort), f"{class_name} does not implement ApplicationServerPort"

    def test_all_adapters_provide_server_info(self):
        """Every available server adapter must expose a valid ServerInfo."""
        valid_names = {name for _, _, name in _SERVER_ADAPTERS}
        for class_name, instance in _available_server_adapters():
            info = instance.server_info  # type: ignore[union-attr]
            assert isinstance(info, ServerInfo), f"{class_name}.server_info is not a ServerInfo instance"
            assert info.name in valid_names, (
                f"{class_name}.server_info.name={info.name!r} is not a recognized server name"
            )

    def test_all_adapters_have_version_string(self):
        """Every server adapter should report a non-empty version string."""
        for class_name, instance in _available_server_adapters():
            info = instance.server_info  # type: ignore[union-attr]
            assert isinstance(info.version, str), f"{class_name}.server_info.version is not a string"
            assert len(info.version) > 0, f"{class_name}.server_info.version is empty"


class TestEventLoopResolution:
    """Test that event loop adapters can be resolved."""

    def test_at_least_one_event_loop_available(self):
        """At least one event loop adapter should be available."""
        adapters = _available_event_loop_adapters()
        assert len(adapters) > 0, "No event loop adapter found in dev environment"

    def test_asyncio_always_available(self):
        """The asyncio fallback adapter must always be importable."""
        from pyfly.server.adapters.event_loop.asyncio_adapter import AsyncioEventLoopAdapter

        adapter = AsyncioEventLoopAdapter()
        assert isinstance(adapter, EventLoopPort)
        assert adapter.name == "asyncio"

    def test_all_available_event_loops_implement_protocol(self):
        """Every importable event loop adapter must satisfy EventLoopPort."""
        for class_name, instance in _available_event_loop_adapters():
            assert isinstance(instance, EventLoopPort), f"{class_name} does not implement EventLoopPort"

    def test_all_event_loops_have_name(self):
        """Every event loop adapter must report a recognized name."""
        valid_names = {name for _, _, name in _EVENT_LOOP_ADAPTERS}
        for class_name, instance in _available_event_loop_adapters():
            assert instance.name in valid_names, (  # type: ignore[union-attr]
                f"{class_name}.name={instance.name!r} is not a recognized event loop name"  # type: ignore[union-attr]
            )


class TestServerProperties:
    """Test that ServerProperties provides sensible defaults."""

    def test_default_values(self):
        """Default ServerProperties should be usable without config files."""
        config = ServerProperties()
        assert config.type == "auto"
        assert config.workers == 1
        assert config.graceful_timeout == 30
        assert config.event_loop == "auto"
        assert config.backlog == 1024

    def test_http_default(self):
        """HTTP protocol should default to auto-detection."""
        config = ServerProperties()
        assert config.http == "auto"

    def test_ssl_defaults_are_none(self):
        """SSL should be disabled by default."""
        config = ServerProperties()
        assert config.ssl_certfile is None
        assert config.ssl_keyfile is None

    def test_granian_sub_properties_defaults(self):
        """Granian-specific defaults should be populated."""
        config = ServerProperties()
        assert config.granian.runtime_threads == 1
        assert config.granian.runtime_mode == "auto"
        assert config.granian.backpressure is None
        assert config.granian.respawn_failed_workers is True


class TestServerInfoImmutability:
    """Test ServerInfo frozen dataclass semantics."""

    def test_is_frozen(self):
        """ServerInfo fields should not be reassignable."""
        info = ServerInfo(
            name="test",
            version="1.0",
            workers=1,
            event_loop="asyncio",
            http_protocol="h1",
            host="localhost",
            port=8000,
        )
        with pytest.raises(AttributeError):
            info.name = "other"  # type: ignore[misc]

    def test_equality(self):
        """Two ServerInfo instances with the same fields should be equal."""
        kwargs = dict(
            name="test",
            version="1.0",
            workers=1,
            event_loop="asyncio",
            http_protocol="h1",
            host="localhost",
            port=8000,
        )
        assert ServerInfo(**kwargs) == ServerInfo(**kwargs)

    def test_all_fields_accessible(self):
        """All declared fields should be readable."""
        info = ServerInfo(
            name="test",
            version="1.0",
            workers=4,
            event_loop="uvloop",
            http_protocol="h2",
            host="127.0.0.1",
            port=9000,
        )
        assert info.name == "test"
        assert info.version == "1.0"
        assert info.workers == 4
        assert info.event_loop == "uvloop"
        assert info.http_protocol == "h2"
        assert info.host == "127.0.0.1"
        assert info.port == 9000


class TestAutoConfigurationMarkers:
    """Test that auto-configuration classes carry the expected markers."""

    def test_server_auto_configuration_is_decorated(self):
        """ServerAutoConfiguration should have the auto-configuration marker."""
        from pyfly.server.auto_configuration import ServerAutoConfiguration

        assert getattr(ServerAutoConfiguration, "__pyfly_auto_configuration__", False)

    def test_event_loop_auto_configuration_is_decorated(self):
        """EventLoopAutoConfiguration should have the auto-configuration marker."""
        from pyfly.server.auto_configuration import EventLoopAutoConfiguration

        assert getattr(EventLoopAutoConfiguration, "__pyfly_auto_configuration__", False)

    def test_server_auto_configuration_beans_are_marked(self):
        """All bean methods in ServerAutoConfiguration should carry the bean marker."""
        from pyfly.server.auto_configuration import ServerAutoConfiguration

        for method_name in ("granian_server", "uvicorn_server", "hypercorn_server"):
            method = getattr(ServerAutoConfiguration, method_name)
            assert getattr(method, "__pyfly_bean__", False), (
                f"ServerAutoConfiguration.{method_name} is missing __pyfly_bean__ marker"
            )

    def test_event_loop_auto_configuration_beans_are_marked(self):
        """All bean methods in EventLoopAutoConfiguration should carry the bean marker."""
        from pyfly.server.auto_configuration import EventLoopAutoConfiguration

        for method_name in ("uvloop", "winloop", "asyncio_loop"):
            method = getattr(EventLoopAutoConfiguration, method_name)
            assert getattr(method, "__pyfly_bean__", False), (
                f"EventLoopAutoConfiguration.{method_name} is missing __pyfly_bean__ marker"
            )
