from __future__ import annotations

import asyncio
import hashlib

import pytest

from pyfly.agentic.sandbox import FunctionInvocation, FunctionLimits, SandboxProfile, SandboxResource
from pyfly.agentic.sandbox.supervisor import FunctionSandbox


def request():
    code = "def main(inputs):\n    return inputs\n"
    return FunctionInvocation(
        "exec-1", code, hashlib.sha256(code.encode()).hexdigest(), {"x": 1}, FunctionLimits(timeout_seconds=1)
    )


def profile():
    return SandboxProfile(
        "local", 1, "docker", "sha256:" + "a" * 64, docker_context="isolated", docker_daemon_id="daemon"
    )


class Session:
    def __init__(self, messages):
        self.messages = list(messages)
        self.writes = []

    async def read_frame(self):
        return self.messages.pop(0) if self.messages else None

    async def write_frame(self, value):
        self.writes.append(value)

    async def wait(self):
        return 0


class Backend:
    def __init__(self, messages, *, cleanup=True):
        self.events = []
        self.cleanup = cleanup
        self.session = Session(messages)

    async def create(self, profile, invocation):
        self.events.append("create")
        return SandboxResource("docker", invocation.execution_id, "a" * 64, profile.id, profile.revision)

    async def start(self, profile, resource):
        self.events.append("start")
        return self.session

    async def stop(self, profile, resource):
        self.events.append("stop")
        return self.cleanup

    async def reconcile(self, profile, resource):
        return self.cleanup


@pytest.mark.asyncio
async def test_known_prelaunch_rejection_has_no_ambiguous_resource():
    from pyfly.agentic.sandbox import PrelaunchRejected

    backend = Backend([])

    async def reject(*args):
        raise PrelaunchRejected()

    async def noop(*args):
        pass

    backend.create = reject
    outcome = await FunctionSandbox(backend).invoke(profile(), request(), effects=None, observe=noop, authorize=noop)
    assert outcome.status == "failed" and outcome.error_code == "POLICY_REJECTED"
    assert outcome.cleanup_confirmed and outcome.resource is None


@pytest.mark.asyncio
async def test_unclassified_creation_error_remains_unknown():
    backend = Backend([])

    async def ambiguous(*args):
        raise RuntimeError("create may have succeeded")

    async def noop(*args):
        pass

    backend.create = ambiguous
    outcome = await FunctionSandbox(backend).invoke(profile(), request(), effects=None, observe=noop, authorize=noop)
    assert outcome.status == "unknown" and outcome.error_code == "CREATE_UNKNOWN"
    assert not outcome.cleanup_confirmed and outcome.resource is None


@pytest.mark.asyncio
async def test_authorizer_rejection_during_inflight_create_stays_unknown():
    from pyfly.agentic.sandbox import PrelaunchRejected

    backend = Backend([])
    release = asyncio.Event()

    async def create(*args):
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    calls = 0

    async def authorize():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise PrelaunchRejected()

    async def noop(*args):
        pass

    backend.create = create
    try:
        outcome = await FunctionSandbox(backend).invoke(
            profile(), request(), effects=None, observe=noop, authorize=authorize
        )
        assert outcome.status == "unknown" and outcome.error_code == "CREATE_UNKNOWN"
        assert not outcome.cleanup_confirmed
    finally:
        release.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_router_dispatches_by_profile_and_rejects_unconfigured_kind():
    from pyfly.agentic.sandbox import RoutingSandboxBackend

    backend = Backend([])
    router = RoutingSandboxBackend({"docker": backend})
    resource = await router.create(profile(), request())
    assert await router.start(profile(), resource) is backend.session
    assert await router.stop(profile(), resource)
    assert await router.reconcile(profile(), resource)
    kube = SandboxProfile(
        "kube",
        1,
        "kubernetes",
        "example.com/image@sha256:" + "a" * 64,
        kubeconfig="/tmp/config",
        kube_context="test",
        namespace="test",
    )
    with pytest.raises(ValueError):
        await router.create(kube, request())


@pytest.mark.asyncio
async def test_observation_and_auth_precede_guest_and_effects():
    backend = Backend(
        [{"type": "effect", "id": 1, "name": "get", "args": {"x": 2}}, {"type": "result", "output": {"ok": True}}]
    )
    events = backend.events

    async def observe(resource):
        events.append("observe")

    async def authorize():
        events.append("authorize")

    async def effects(name, args):
        events.append("effect")
        return {"answer": 3}

    outcome = await FunctionSandbox(backend).invoke(
        profile(), request(), effects=effects, observe=observe, authorize=authorize
    )
    assert outcome.status == "succeeded" and outcome.cleanup_confirmed
    assert events[:4] == ["authorize", "create", "observe", "authorize"]
    assert events.index("start") > events.index("observe")
    assert events.index("effect") > events.index("start")
    assert backend.session.writes[-1] == {"type": "effect_result", "id": 1, "value": {"answer": 3}}


@pytest.mark.asyncio
async def test_unconfirmed_cleanup_overrides_success():
    backend = Backend([{"type": "result", "output": 5}], cleanup=False)

    async def noop(*args):
        pass

    outcome = await FunctionSandbox(backend).invoke(profile(), request(), effects=None, observe=noop, authorize=noop)
    assert outcome.status == "unknown" and not outcome.cleanup_confirmed and outcome.output is None


@pytest.mark.asyncio
async def test_forged_duplicate_effect_id_fails_closed():
    frame = {"type": "effect", "id": 1, "name": "get", "args": {}}
    backend = Backend([frame, frame])

    async def noop(*args):
        pass

    async def effect(*args):
        return {}

    outcome = await FunctionSandbox(backend).invoke(profile(), request(), effects=effect, observe=noop, authorize=noop)
    assert outcome.status == "failed" and outcome.error_code == "PROTOCOL_ERROR"


@pytest.mark.asyncio
async def test_effect_ignoring_cancellation_returns_unknown_without_hanging():
    backend = Backend([{"type": "effect", "id": 1, "name": "get", "args": {}}])
    started, release = asyncio.Event(), asyncio.Event()

    async def noop(*args):
        pass

    async def effect(*args):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    req = request()
    req = FunctionInvocation(
        req.execution_id, req.source, req.source_sha256, req.inputs, FunctionLimits(timeout_seconds=0.1)
    )
    try:
        outcome = await asyncio.wait_for(
            FunctionSandbox(backend).invoke(profile(), req, effects=effect, observe=noop, authorize=noop), 1
        )
        assert started.is_set()
        assert outcome.status == "unknown" and outcome.error_code == "EFFECT_UNKNOWN"
    finally:
        release.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_unconfirmed_create_returns_unknown_without_hanging():
    backend = Backend([])
    entered, release = asyncio.Event(), asyncio.Event()

    async def stuck(*args):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return SandboxResource("docker", "exec-1", "a" * 64, "local", 1)

    backend.create = stuck

    async def noop(*args):
        pass

    req = request()
    req = FunctionInvocation(
        req.execution_id, req.source, req.source_sha256, req.inputs, FunctionLimits(timeout_seconds=0.1)
    )
    try:
        started_at = asyncio.get_running_loop().time()
        outcome = await asyncio.wait_for(
            FunctionSandbox(backend).invoke(profile(), req, effects=None, observe=noop, authorize=noop), 1
        )
        assert asyncio.get_running_loop().time() - started_at < 0.5
        assert entered.is_set()
        assert outcome.status == "unknown" and not outcome.cleanup_confirmed and outcome.resource is None
    finally:
        release.set()
        await asyncio.sleep(0)
