from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from pyfly.agentic.sandbox import FunctionInvocation, FunctionLimits, SandboxProfile


async def prepare(profile, execution_id):
    return profile


async def release(profile, execution_id):
    pass


@pytest.mark.asyncio
async def test_kubernetes_manifest_and_uid_preconditioned_termination():
    from pyfly.agentic.sandbox import KubernetesSandboxBackend

    class Api:
        def __init__(self):
            self.deleted = None
            self.pod = None

        def create_namespaced_pod(self, namespace, body, **kwargs):
            self.pod = body
            body["metadata"]["uid"] = "11111111-1111-4111-8111-111111111111"
            body["status"] = {"phase": "Running"}
            return body

        def read_namespaced_pod(self, name, namespace, **kwargs):
            if self.deleted:
                raise SimpleNamespaceException(404)
            return self.pod

        def delete_namespaced_pod(self, name, namespace, body, **kwargs):
            self.deleted = body

    class SimpleNamespaceException(Exception):
        def __init__(self, status):
            self.status = status

    api = Api()
    snapshots = []
    releases = []

    async def prepare_snapshot(profile, execution_id):
        return replace(profile, kubeconfig="/protected/snapshot")

    async def release_snapshot(profile, execution_id):
        releases.append((profile.kubeconfig, execution_id))

    async def verify(profile):
        snapshots.append(profile.kubeconfig)
        return True

    def factory(profile):
        assert profile.kubeconfig == "/protected/snapshot"
        return api

    backend = KubernetesSandboxBackend(
        prepare_profile=prepare_snapshot,
        release_profile=release_snapshot,
        verify_isolation=verify,
        api_factory=factory,
        terminal_wait=0,
    )
    profile = SandboxProfile(
        "safe",
        1,
        "kubernetes",
        "repo/image@sha256:" + "a" * 64,
        kubeconfig="/tmp/config",
        kube_context="ctx",
        namespace="isolated",
    )
    source = "def main(inputs): return inputs"
    invocation = FunctionInvocation("e1", source, hashlib.sha256(source.encode()).hexdigest(), {}, FunctionLimits())
    resource = await backend.create(profile, invocation)
    assert resource.id == "11111111-1111-4111-8111-111111111111"
    spec = api.pod["spec"]
    assert spec["automountServiceAccountToken"] is False and spec["restartPolicy"] == "Never"
    assert spec["containers"][0]["securityContext"]["readOnlyRootFilesystem"] is True
    assert spec["containers"][0]["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert backend._safe_pod(profile, api.pod, backend._specs[resource.id])
    api.pod["spec"]["containers"].append({"name": "injected"})
    assert not backend._safe_pod(profile, api.pod, backend._specs[resource.id])
    api.pod["spec"]["containers"].pop()
    assert not await backend.stop(profile, resource)
    api.pod["metadata"]["uid"] = "22222222-2222-4222-8222-222222222222"
    assert not await backend.stop(profile, resource)
    assert api.deleted is None
    api.pod["metadata"]["uid"] = resource.id
    api.pod["status"] = {
        "phase": "Succeeded",
        "containerStatuses": [{"name": "runner", "state": {"terminated": {"exitCode": 0}}}],
    }
    recovered = KubernetesSandboxBackend(
        prepare_profile=prepare_snapshot,
        release_profile=release_snapshot,
        verify_isolation=verify,
        api_factory=factory,
        terminal_wait=0,
    )
    assert await recovered.reconcile(profile, resource)
    assert api.deleted["preconditions"]["uid"] == resource.id
    assert snapshots and set(snapshots) == {"/protected/snapshot"}
    assert releases == [("/protected/snapshot", "e1")]


@pytest.mark.asyncio
async def test_kubernetes_policy_is_required():
    from pyfly.agentic.sandbox import KubernetesSandboxBackend

    released = []

    async def deny(profile):
        return False

    async def release_denied(profile, execution_id):
        released.append(execution_id)

    backend = KubernetesSandboxBackend(
        prepare_profile=prepare, release_profile=release_denied, verify_isolation=deny, api_factory=lambda profile: None
    )
    profile = SandboxProfile(
        "safe",
        1,
        "kubernetes",
        "repo/image@sha256:" + "a" * 64,
        kubeconfig="/tmp/config",
        kube_context="ctx",
        namespace="isolated",
    )
    source = "def main(inputs): return inputs"
    request = FunctionInvocation("e1", source, hashlib.sha256(source.encode()).hexdigest(), {}, FunctionLimits())
    with pytest.raises(ValueError):
        await backend.create(profile, request)
    assert released == ["e1"]


@pytest.mark.asyncio
async def test_snapshot_release_rejection_cannot_claim_confirmed_cleanup():
    from pyfly.agentic.sandbox import FunctionSandbox, KubernetesSandboxBackend, PrelaunchRejected

    async def deny(profile):
        return False

    async def release_fails(profile, execution_id):
        raise PrelaunchRejected()

    async def noop(*args):
        pass

    backend = KubernetesSandboxBackend(
        prepare_profile=prepare, release_profile=release_fails, verify_isolation=deny, api_factory=lambda profile: None
    )
    profile = SandboxProfile(
        "safe",
        1,
        "kubernetes",
        "repo/image@sha256:" + "a" * 64,
        kubeconfig="/tmp/config",
        kube_context="ctx",
        namespace="isolated",
    )
    source = "def main(inputs): return inputs"
    request = FunctionInvocation("e1", source, hashlib.sha256(source.encode()).hexdigest(), {}, FunctionLimits())
    outcome = await FunctionSandbox(backend).invoke(profile, request, effects=None, observe=noop, authorize=noop)
    assert outcome.status == "unknown" and outcome.error_code == "CREATE_UNKNOWN" and not outcome.cleanup_confirmed


@pytest.mark.asyncio
async def test_recovery_denial_retains_existing_snapshot():
    from pyfly.agentic.sandbox import KubernetesSandboxBackend, SandboxResource

    released = []

    async def deny(profile):
        return False

    async def release_denied(profile, execution_id):
        released.append(execution_id)

    backend = KubernetesSandboxBackend(
        prepare_profile=prepare, release_profile=release_denied, verify_isolation=deny, api_factory=lambda profile: None
    )
    profile = SandboxProfile(
        "safe",
        1,
        "kubernetes",
        "repo/image@sha256:" + "a" * 64,
        kubeconfig="/tmp/config",
        kube_context="ctx",
        namespace="isolated",
    )
    resource = SandboxResource("kubernetes", "e1", "uid", "safe", 1)
    assert not await backend.reconcile(profile, resource)
    assert released == []


@pytest.mark.asyncio
async def test_replaced_pod_cannot_receive_source(monkeypatch):
    from types import SimpleNamespace

    import pyfly.agentic.sandbox.kubernetes as module
    from pyfly.agentic.sandbox import KubernetesSandboxBackend, SandboxResource

    async def verify(profile):
        return True

    backend = KubernetesSandboxBackend(
        prepare_profile=prepare,
        release_profile=release,
        verify_isolation=verify,
        api_factory=lambda profile: SimpleNamespace(connect_get_namespaced_pod_portforward=None),
    )
    profile = SandboxProfile(
        "safe",
        1,
        "kubernetes",
        "repo/image@sha256:" + "a" * 64,
        kubeconfig="/tmp/config",
        kube_context="ctx",
        namespace="isolated",
    )
    resource = SandboxResource("kubernetes", "e1", "uid", "safe", 1)
    backend._profiles["uid"] = profile
    backend._limits["uid"] = 1024
    backend._buffer_limits["uid"] = 2048
    backend._specs["uid"] = {}
    reads = []
    writes = []

    async def read(api, profile, resource):
        reads.append(True)
        if len(reads) == 2:
            raise ValueError("UID changed")
        return {"status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}

    class Forward:
        closed = False

        def socket(self, port):
            return self

        def dup(self):
            return self

        def setblocking(self, value):
            pass

        def close(self):
            self.closed = True

    forwarding = Forward()

    async def connect(*args, **kwargs):
        return None, None

    class Session:
        def __init__(self, forwarding, reader, writer, limit, terminal):
            self.forwarding = forwarding

        async def write_frame(self, value):
            writes.append(value)

        async def read_frame(self):
            return {"type": "ready"}

        async def close(self):
            self.forwarding.close()

    monkeypatch.setattr(backend, "_read", read)
    monkeypatch.setattr(backend, "_safe_pod", lambda *args: True)
    monkeypatch.setattr(module, "_open_bounded_forward", lambda *args, **kwargs: forwarding)
    monkeypatch.setattr(module.asyncio, "open_connection", connect)
    monkeypatch.setattr(module, "KubernetesSession", Session)
    with pytest.raises(ValueError, match="UID changed"):
        await backend.start(profile, resource)
    assert writes == [{"type": "probe"}] and forwarding.closed


def test_websocket_proxy_caps_untrusted_data_and_error_buffers():
    from pyfly.agentic.sandbox.kubernetes import _bounded_forward_type

    forward_type = _bounded_forward_type(64)
    port = forward_type._Port(0, 8765)
    try:
        port.data = b"x" * 64
        with pytest.raises(ValueError):
            port.data += b"x"
        port.error = "x" * 64
        with pytest.raises(ValueError):
            port.error += "x"
    finally:
        port.socket.close()
        port.python.close()


@pytest.mark.asyncio
async def test_forwarding_caps_cumulative_host_frames():
    from pyfly.agentic.sandbox.framing import FrameError
    from pyfly.agentic.sandbox.kubernetes import KubernetesSession

    class Writer:
        def write(self, data):
            pass

        async def drain(self):
            pass

    async def terminal():
        return 0

    session = KubernetesSession(None, None, Writer(), 64, terminal)
    session.sent = 2097152 + 64 - 5
    with pytest.raises(FrameError):
        await session.write_frame({"type": "probe"})


def test_websocket_proxy_closes_when_consumer_is_backpressured(monkeypatch):
    from types import SimpleNamespace

    from kubernetes.stream import ws_client
    from websocket import ABNF

    from pyfly.agentic.sandbox.kubernetes import _bounded_forward_type

    class FakeWebSocket:
        connected = True
        sock = None
        frames = 0

        def recv_data_frame(self, control):
            self.frames += 1
            data = b"\x00\x3d\x22" if self.frames == 1 else b"\x00" + b"x" * 8192
            return ABNF.OPCODE_BINARY, SimpleNamespace(data=data)

        def close(self, **kwargs):
            self.connected = False

    ws = FakeWebSocket()
    forward_type = _bounded_forward_type(65536)
    forward = forward_type.__new__(forward_type)
    forward.websocket = ws
    port = forward_type._Port(0, 8765)
    forward.local_ports = {8765: port}
    monkeypatch.setattr(ws_client.select, "select", lambda r, w, x: ([ws], [], []))
    try:
        forward._proxy()
        assert not ws.connected
        assert len(port.data) <= 65536
        assert port.python.fileno() == -1
    finally:
        port.socket.close()
        port.python.close()


@pytest.mark.asyncio
async def test_cancelled_forward_setup_retires_late_connection(monkeypatch):
    import asyncio
    import threading

    import pyfly.agentic.sandbox.kubernetes as module
    from pyfly.agentic.sandbox import KubernetesSandboxBackend, SandboxResource

    entered, resume, closed = threading.Event(), threading.Event(), threading.Event()

    class Forward:
        def close(self):
            closed.set()

    def open_late(*args):
        entered.set()
        resume.wait(2)
        return Forward()

    async def verify(profile):
        return True

    backend = KubernetesSandboxBackend(
        prepare_profile=prepare, release_profile=release, verify_isolation=verify, api_factory=lambda profile: None
    )
    profile = SandboxProfile(
        "safe",
        1,
        "kubernetes",
        "repo/image@sha256:" + "a" * 64,
        kubeconfig="/tmp/config",
        kube_context="ctx",
        namespace="isolated",
    )
    resource = SandboxResource("kubernetes", "e1", "uid", "safe", 1)
    backend._profiles["uid"] = profile
    backend._limits["uid"] = 1024
    backend._buffer_limits["uid"] = 2048
    backend._specs["uid"] = {}

    async def read(*args):
        return {"status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}

    monkeypatch.setattr(backend, "_read", read)
    monkeypatch.setattr(backend, "_safe_pod", lambda *args: True)
    monkeypatch.setattr(module, "_open_bounded_forward", open_late)
    task = asyncio.create_task(backend.start(profile, resource))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        resume.set()
        assert await asyncio.to_thread(closed.wait, 1)
    finally:
        resume.set()
