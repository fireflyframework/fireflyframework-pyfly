"""Failed/cancelled refreshes release resources without a second caller-owned stop."""

import asyncio
import warnings

import pytest
from pydantic import BaseModel
from pydantic.warnings import PydanticDeprecatedSince211

from pyfly.container import bean, configuration
from pyfly.container.exceptions import BeanCreationException
from pyfly.context.application_context import ApplicationContext
from pyfly.context.lifecycle import post_construct, pre_destroy
from pyfly.core.config import Config


@pytest.fixture(autouse=True)
def no_auto_configurations(monkeypatch):
    monkeypatch.setattr("pyfly.config.auto.discover_auto_configurations", lambda: [])


@pytest.mark.parametrize("cancel", [False, True])
async def test_partial_start_releases_resources_and_preserves_failure(cancel):
    events = []
    allocated = asyncio.Event()

    class Resource:
        async def start(self):
            events.append("allocate")
            allocated.set()
            if cancel:
                await asyncio.Event().wait()
            raise ValueError("startup broke")

        async def stop(self):
            events.append("release")
            raise RuntimeError("cleanup broke")

        @pre_destroy
        def cleanup(self):
            events.append("destroy")

    ctx = ApplicationContext(Config({}))
    ctx.register_bean(Resource)
    task = asyncio.create_task(ctx.start())
    await allocated.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else BeanCreationException) as raised:
        await task
    if not cancel:
        assert isinstance(raised.value.__cause__, ValueError)
    assert events == ["allocate", "destroy", "release"]
    await ctx.stop()
    assert events == ["allocate", "destroy", "release"]


async def test_failed_start_calls_all_cleanup_hooks_after_timeout_and_cancelled_hook():
    events = []

    class Resource:
        @post_construct
        def init(self):
            raise ValueError("startup broke")

        @pre_destroy
        async def a_hangs(self):
            events.append("timeout")
            await asyncio.Event().wait()

        @pre_destroy
        async def b_cancels(self):
            events.append("cancel")
            raise asyncio.CancelledError

        @pre_destroy
        async def c_closes(self):
            events.append("closed")

    ctx = ApplicationContext(Config({"pyfly": {"context": {"shutdown-timeout": 0.01}}}))
    ctx.register_bean(Resource)
    with pytest.raises(BeanCreationException):
        await ctx.start()
    assert events == ["timeout", "cancel", "closed"]


async def test_marked_start_stop_and_explicit_destroy_are_not_called_twice():
    events = []

    class Resource:
        @post_construct
        async def start(self):
            events.append("start")

        @pre_destroy
        async def stop(self):
            events.append("stop")

    @configuration
    class Resources:
        @bean(destroy_method="stop")
        def resource(self) -> Resource:
            return Resource()

    ctx = ApplicationContext(Config({}))
    ctx.register_bean(Resources)
    await ctx.start()
    await ctx.stop()
    await ctx.stop()
    assert events == ["start", "stop"]


async def test_failed_factory_run_can_restart_without_stale_aliases():
    resources = []
    fail = True

    class Resource:
        closed = False

        def close(self):
            self.closed = True

    @configuration
    class Resources:
        @bean(destroy_method="close")
        def resource(self) -> Resource:
            resource = Resource()
            resources.append(resource)
            return resource

    class Runner:
        @post_construct
        def init(self):
            if fail:
                raise ValueError("startup broke")

    ctx = ApplicationContext(Config({}))
    ctx.register_bean(Resources)
    ctx.register_bean(Runner)
    with pytest.raises(BeanCreationException):
        await ctx.start()
    assert len(resources) == 1 and resources[0].closed
    fail = False
    await ctx.start()
    assert len(resources) == 2 and not resources[1].closed
    assert ctx.get_bean(Resource) is resources[1]
    await ctx.stop()
    assert resources[1].closed


def test_safe_members_does_not_evaluate_pydantic_deprecated_descriptors():
    class Settings(BaseModel):
        name: str = "test"

        def handler(self):
            return self.name

    settings = Settings()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", PydanticDeprecatedSince211)
        members = dict(ApplicationContext._safe_members(settings))
    assert members["handler"]() == "test"
    assert not caught


async def test_decorated_start_failure_stops_its_partially_started_resource():
    events = []

    class Resource:
        @post_construct
        async def start(self):
            events.append("allocate")
            raise ValueError("startup broke")

        async def stop(self):
            events.append("release")

    ctx = ApplicationContext(Config({}))
    ctx.register_bean(Resource)
    with pytest.raises(BeanCreationException):
        await ctx.start()
    assert events == ["allocate", "release"]


async def test_package_discovery_preserves_service_constructor_injection_and_port_aliases(tmp_path, monkeypatch):
    import importlib

    from pyfly.container.scanner import scan_package

    package = tmp_path / "native_service_fixture"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "services.py").write_text("""
from typing import Protocol, runtime_checkable
from pyfly.container import service, configuration, bean
from pyfly.context.lifecycle import post_construct, pre_destroy

@runtime_checkable
class Port(Protocol):
    def read(self) -> str: ...

class UserInfrastructure:
    def read(self) -> str:
        return "user infrastructure"

@configuration
class Infrastructure:
    @bean
    def storage(self) -> Port:
        return UserInfrastructure()

@service
class NativeService:
    def __init__(self, storage: Port):
        self.storage = storage
        self.calls = []

    @post_construct
    def ready(self):
        self.calls.append(self.storage.read())

    @pre_destroy
    def close(self):
        self.calls.append("closed")
""")
    monkeypatch.syspath_prepend(str(tmp_path))
    module = importlib.import_module("native_service_fixture.services")
    ctx = ApplicationContext(Config({}))
    assert scan_package("native_service_fixture", ctx.container) == 2
    await ctx.start()
    service = ctx.get_bean(module.NativeService)
    assert service.storage is ctx.get_bean(module.Port)
    assert service.storage is ctx.get_bean(module.UserInfrastructure)
    assert service.calls == ["user infrastructure"]
    await ctx.stop()
    assert service.calls == ["user infrastructure", "closed"]


async def test_self_cancelled_registry_does_not_prevent_other_registry_cleanup():
    events = []

    class HealthyRegistry:
        async def dispose_all(self):
            events.append("healthy")

    class CancelledRegistry:
        async def dispose_all(self):
            events.append("cancelled")
            raise asyncio.CancelledError

    ctx = ApplicationContext(Config({}))
    ctx.register_bean(HealthyRegistry)
    ctx.register_bean(CancelledRegistry)
    await ctx.start()
    await ctx.stop()
    assert events == ["cancelled", "healthy"]
    await ctx.stop()
    assert events == ["cancelled", "healthy"]


async def test_cancellation_resistant_cleanup_cannot_block_other_beans():
    release = asyncio.Event()
    finished = asyncio.Event()
    events = []

    class Resource:
        @pre_destroy
        async def a_resists_cancellation(self):
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finally:
                finished.set()

        @pre_destroy
        async def b_closes(self):
            events.append("closed")

    ctx = ApplicationContext(Config({"pyfly": {"context": {"shutdown-timeout": 0.01}}}))
    ctx.register_bean(Resource)
    await ctx.start()
    task = asyncio.create_task(ctx.stop())
    try:
        done, _ = await asyncio.wait({task}, timeout=0.2)
        assert done, "cleanup did not respect its timeout"
        await task
        assert events == ["closed"]
    finally:
        release.set()
        await finished.wait()
        await task


async def test_external_stop_cancellation_finishes_other_cleanup_then_propagates():
    entered = asyncio.Event()
    events = []

    class Resource:
        @pre_destroy
        async def a_waits(self):
            entered.set()
            await asyncio.Event().wait()

        @pre_destroy
        async def b_closes(self):
            events.append("closed")

    ctx = ApplicationContext(Config({}))
    ctx.register_bean(Resource)
    await ctx.start()
    task = asyncio.create_task(ctx.stop())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["closed"]
    await ctx.stop()
    assert events == ["closed"]
