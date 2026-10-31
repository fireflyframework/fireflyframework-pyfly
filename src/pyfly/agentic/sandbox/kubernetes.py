# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Kubernetes control plane for isolated function pods."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

from pyfly.agentic.sandbox.contracts import (
    FunctionInvocation,
    PrelaunchRejected,
    SandboxProfile,
    SandboxResource,
    encode_json,
)
from pyfly.agentic.sandbox.framing import FrameError, read_frame, write_frame

_GUEST_PORT = 8765
_RUNNER = [
    "python",
    "-I",
    "-u",
    "/opt/pyfly/runner.py",
    "--socket",
    str(_GUEST_PORT),
    "--ready-file",
    "/tmp/pyfly-ready",
]
_READY_COMMAND = ["test", "-f", "/tmp/pyfly-ready"]


def _bounded_forward_type(limit: int) -> Any:
    from kubernetes.stream.ws_client import PortForward

    class BoundedPort(PortForward._Port):
        def __setattr__(self, name: str, value: Any) -> None:
            if name in ("data", "error") and value is not None and len(value) > limit:
                raise ValueError("Forwarding buffer exceeds limit")
            super().__setattr__(name, value)

    class BoundedForward(PortForward):
        _Port = BoundedPort

        def close(self) -> None:
            for port in self.local_ports.values():
                port.socket.close()
                port.python.close()
            self.websocket.close(timeout=1)

        def _proxy(self) -> None:
            try:
                super()._proxy()  # type: ignore[no-untyped-call]
            except BaseException:
                for port in self.local_ports.values():
                    port.python.close()
                self.websocket.close(timeout=1)

    return BoundedForward


def _open_bounded_forward(api: Any, profile: SandboxProfile, resource: SandboxResource, limit: int) -> Any:
    import ssl
    from urllib.parse import parse_qsl, urlparse

    import certifi
    from kubernetes.stream import portforward
    from kubernetes.stream.ws_client import get_websocket_url, websocket_proxycare
    from websocket import WebSocket

    def bounded_call(configuration: Any, _method: str, url: str, **kwargs: Any) -> Any:
        query = kwargs.get("query_params") or parse_qsl(urlparse(url).query, keep_blank_values=True)
        if [value for key, value in query if key == "ports"] != [str(_GUEST_PORT)]:
            raise ValueError("Invalid forwarding port")
        target = get_websocket_url(url, query)  # type: ignore[no-untyped-call]
        if not target.startswith("wss://") or not configuration.verify_ssl:
            raise ValueError("Verified Kubernetes TLS is required")
        headers = kwargs.get("headers") or {}
        header = []
        if "authorization" in headers:
            header.append("authorization: " + headers["authorization"])
        header.append("sec-websocket-protocol: v5.channel.k8s.io,v4.channel.k8s.io")
        ssl_options: dict[str, Any] = {
            "cert_reqs": ssl.CERT_REQUIRED,
            "ca_certs": configuration.ssl_ca_cert or certifi.where(),
        }
        if configuration.assert_hostname is not None:
            ssl_options["check_hostname"] = configuration.assert_hostname
        if configuration.cert_file:
            ssl_options["certfile"] = configuration.cert_file
        if configuration.key_file:
            ssl_options["keyfile"] = configuration.key_file
        if configuration.tls_server_name:
            ssl_options["server_hostname"] = configuration.tls_server_name
        options: dict[str, Any] = {"header": header, "timeout": 5}
        if configuration.proxy or configuration.proxy_headers:
            options = websocket_proxycare(options, configuration, target, headers)  # type: ignore[no-untyped-call]
            options["http_proxy_timeout"] = 5
        websocket = WebSocket(sslopt=ssl_options, skip_utf8_validation=False)
        websocket.settimeout(5)
        try:
            websocket.connect(target, **options)  # type: ignore[no-untyped-call]
        except BaseException:
            websocket.close()
            raise
        return _bounded_forward_type(limit)(websocket, [_GUEST_PORT])

    # Reuse the official client's request serialization and TLS/auth configuration.
    return portforward.func(
        bounded_call,
        {"_preload_content": False},
        api.connect_get_namespaced_pod_portforward,
        _name(resource.execution_id),
        profile.namespace,
        ports=str(_GUEST_PORT),
        _request_timeout=5,
    )


class KubernetesSession:
    def __init__(
        self,
        forwarding: Any,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        limit: int,
        terminal: Callable[[], Awaitable[int]],
    ) -> None:
        self.forwarding, self.reader, self.writer, self.limit, self.terminal = (
            forwarding,
            reader,
            writer,
            limit,
            terminal,
        )
        self.used = 0
        self.sent = 0

    async def read_frame(self) -> Any:
        value = await read_frame(self.reader, limit=self.limit)
        self.used += len(encode_json(value, limit=self.limit))
        if self.used > self.limit:
            raise FrameError("Guest output exceeds limit")
        return value

    async def write_frame(self, value: Any) -> None:
        self.sent += 4 + len(encode_json(value, limit=2097152))
        if self.sent > 2097152 + self.limit:
            raise FrameError("Sandbox input stream exceeds limit")
        await write_frame(self.writer, value, limit=2097152)

    async def wait(self) -> int:
        if await self.reader.read(1):
            raise FrameError("Trailing guest output")
        return await self.terminal()

    async def close(self) -> None:
        self.writer.close()
        with contextlib.suppress(Exception):
            await self.writer.wait_closed()
        self.forwarding.close()


def _default_api(profile: SandboxProfile) -> Any:
    try:
        from kubernetes import client, config
    except ImportError as exc:
        raise RuntimeError("Install pyfly[agentic-sandbox-kubernetes]") from exc
    api_client = config.new_client_from_config(config_file=profile.kubeconfig, context=profile.kube_context)  # type: ignore[no-untyped-call]
    return client.CoreV1Api(api_client)


def _dict(api: Any, value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return cast(dict[str, Any], api.api_client.sanitize_for_serialization(value))


def _name(execution_id: str) -> str:
    return "pyfly-fn-" + hashlib.sha256(execution_id.encode()).hexdigest()[:40]


class KubernetesSandboxBackend:
    """Run in a verified namespace; never treat missing pods as proof of termination."""

    def __init__(
        self,
        *,
        verify_isolation: Callable[[SandboxProfile], Awaitable[bool]],
        prepare_profile: Callable[[SandboxProfile, str], Awaitable[SandboxProfile]],
        release_profile: Callable[[SandboxProfile, str], Awaitable[None]],
        api_factory: Callable[[SandboxProfile], Any] | None = None,
        terminal_wait: float = 52.0,
    ) -> None:
        if not 0 <= terminal_wait <= 55:
            raise ValueError("Invalid termination wait")
        self._verify_isolation = verify_isolation
        self._prepare_profile = prepare_profile
        self._release_profile = release_profile
        self._api_factory = api_factory or _default_api
        self._terminal_wait = terminal_wait
        self._limits: dict[str, int] = {}
        self._buffer_limits: dict[str, int] = {}
        self._specs: dict[str, dict[str, Any]] = {}
        self._sessions: dict[str, KubernetesSession] = {}
        self._profiles: dict[str, SandboxProfile] = {}

    async def _prepared(self, profile: SandboxProfile, execution_id: str, *, release_on_denial: bool) -> SandboxProfile:
        prepared = await self._prepare_profile(profile, execution_id)
        try:
            if (
                prepared.kind != "kubernetes"
                or (prepared.id, prepared.revision, prepared.image, prepared.kube_context, prepared.namespace)
                != (profile.id, profile.revision, profile.image, profile.kube_context, profile.namespace)
                or not await self._verify_isolation(prepared)
            ):
                raise PrelaunchRejected("Kubernetes connection or namespace is not verified")
        except Exception as exc:
            if release_on_denial:
                try:
                    await self._release_profile(prepared, execution_id)
                except Exception as release_exc:
                    raise RuntimeError("Sandbox configuration cleanup is unconfirmed") from release_exc
            raise PrelaunchRejected("Kubernetes connection or namespace is not verified") from exc
        return prepared

    @staticmethod
    def _identity(profile: SandboxProfile, resource: SandboxResource) -> None:
        if (profile.kind, profile.id, profile.revision) != (
            resource.kind,
            resource.profile_id,
            resource.profile_revision,
        ):
            raise ValueError("Sandbox identity mismatch")

    async def _api(self, profile: SandboxProfile) -> Any:
        return await asyncio.to_thread(self._api_factory, profile)

    async def _read(self, api: Any, profile: SandboxProfile, resource: SandboxResource) -> dict[str, Any]:
        pod = await asyncio.to_thread(
            api.read_namespaced_pod, _name(resource.execution_id), profile.namespace, _request_timeout=5
        )
        data = _dict(api, pod)
        metadata = data.get("metadata", {})
        labels = metadata.get("labels", {})
        if (
            metadata.get("uid") != resource.id
            or labels.get("pyfly-sandbox-profile") != profile.id
            or labels.get("pyfly-sandbox-revision") != str(profile.revision)
            or labels.get("pyfly-sandbox-execution") != hashlib.sha256(resource.execution_id.encode()).hexdigest()[:40]
        ):
            raise ValueError("Sandbox pod identity mismatch")
        return data

    @staticmethod
    def _safe_pod(profile: SandboxProfile, pod: dict[str, Any], expected: dict[str, Any]) -> bool:
        from kubernetes.utils.quantity import parse_quantity as untyped_quantity

        def parse_quantity(value: Any) -> Any:
            return untyped_quantity(value)  # type: ignore[no-untyped-call]

        spec = pod.get("spec", {})
        containers = spec.get("containers") or []
        if (
            len(containers) != 1
            or spec.get("automountServiceAccountToken") is not False
            or spec.get("restartPolicy") != "Never"
            or spec.get("activeDeadlineSeconds") != expected["deadline"]
            or spec.get("terminationGracePeriodSeconds") != 1
            or any(
                spec.get(key)
                for key in (
                    "initContainers",
                    "ephemeralContainers",
                    "hostNetwork",
                    "hostPID",
                    "hostIPC",
                    "shareProcessNamespace",
                )
            )
        ):
            return False
        container = containers[0]
        security = container.get("securityContext") or {}
        pod_security = spec.get("securityContext") or {}
        resources = container.get("resources") or {}
        try:
            resources_match = set(resources) == {"limits", "requests"} and all(
                set(resources[group]) == {"cpu", "memory"}
                and all(
                    parse_quantity(resources[group][key]) == parse_quantity(expected["resources"][group][key])
                    for key in ("cpu", "memory")
                )
                for group in ("limits", "requests")
            )
            tmp_matches = parse_quantity((spec.get("volumes") or [{}])[0].get("emptyDir", {}).get("sizeLimit")) == (
                parse_quantity(expected["tmp"])
            )
        except (KeyError, TypeError, ValueError):
            return False
        return bool(
            container.get("name") == "runner"
            and container.get("image") == profile.image
            and container.get("command") == _RUNNER
            and not container.get("stdin")
            and not container.get("stdinOnce")
            and not container.get("tty")
            and resources_match
            and not container.get("args")
            and not container.get("env")
            and not container.get("envFrom")
            and not container.get("lifecycle")
            and not container.get("livenessProbe")
            and container.get("readinessProbe", {}).get("exec", {}).get("command") == _READY_COMMAND
            and not container.get("startupProbe")
            and not container.get("volumeDevices")
            and not container.get("securityContext", {}).get("privileged")
            and security.get("runAsUser") in (None, 65532)
            and security.get("runAsGroup") in (None, 65532)
            and security.get("runAsNonRoot") is not False
            and security.get("seccompProfile", {}).get("type", "RuntimeDefault") == "RuntimeDefault"
            and security.get("allowPrivilegeEscalation") is False
            and security.get("readOnlyRootFilesystem") is True
            and security.get("capabilities", {}).get("drop") == ["ALL"]
            and not security.get("capabilities", {}).get("add")
            and pod_security.get("runAsNonRoot") is True
            and pod_security.get("runAsUser") == 65532
            and pod_security.get("runAsGroup") == 65532
            and pod_security.get("seccompProfile", {}).get("type") == "RuntimeDefault"
            and len(spec.get("volumes") or []) == 1
            and (spec.get("volumes") or [{}])[0].get("name") == "tmp"
            and (spec.get("volumes") or [{}])[0].get("emptyDir", {}).get("medium") == "Memory"
            and tmp_matches
            and container.get("volumeMounts") == [{"name": "tmp", "mountPath": "/tmp"}]
        )

    async def create(self, profile: SandboxProfile, invocation: FunctionInvocation) -> SandboxResource:
        if profile.kind != "kubernetes" or invocation.limits.timeout_seconds > 30:
            raise PrelaunchRejected("Kubernetes sandbox requires a timeout at most 30 seconds")
        profile = await self._prepared(profile, invocation.execution_id, release_on_denial=True)
        limits = invocation.limits
        labels = {
            "pyfly-sandbox-profile": profile.id,
            "pyfly-sandbox-revision": str(profile.revision),
            "pyfly-sandbox-execution": hashlib.sha256(invocation.execution_id.encode()).hexdigest()[:40],
        }
        pod: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": _name(invocation.execution_id), "labels": labels},
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "enableServiceLinks": False,
                "terminationGracePeriodSeconds": 1,
                "activeDeadlineSeconds": math.ceil(limits.timeout_seconds) + 15,
                "securityContext": {
                    "runAsNonRoot": True,
                    "runAsUser": 65532,
                    "runAsGroup": 65532,
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "containers": [
                    {
                        "name": "runner",
                        "image": profile.image,
                        "command": _RUNNER,
                        "readinessProbe": {
                            "exec": {"command": _READY_COMMAND},
                            "periodSeconds": 1,
                            "timeoutSeconds": 1,
                            "failureThreshold": 3,
                        },
                        "tty": False,
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "resources": {
                            "requests": {"memory": str(limits.memory_bytes), "cpu": "100m"},
                            "limits": {"memory": str(limits.memory_bytes), "cpu": "500m"},
                        },
                        "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
                    }
                ],
                "volumes": [{"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": str(limits.tmp_bytes)}}],
            },
        }
        api = await self._api(profile)
        result = await asyncio.to_thread(api.create_namespaced_pod, profile.namespace, pod, _request_timeout=5)
        data = _dict(api, result)
        uid = data.get("metadata", {}).get("uid")
        if not isinstance(uid, str) or not uid:
            raise RuntimeError("Pod creation outcome unknown")
        resource = SandboxResource("kubernetes", invocation.execution_id, uid, profile.id, profile.revision)
        self._limits[uid] = limits.max_output_bytes
        self._buffer_limits[uid] = limits.max_output_bytes + 4 * (limits.max_frames + 1) + 1024
        self._profiles[uid] = profile
        self._specs[uid] = {
            "resources": copy.deepcopy(pod["spec"]["containers"][0]["resources"]),
            "tmp": str(limits.tmp_bytes),
            "deadline": math.ceil(limits.timeout_seconds) + 15,
        }
        return resource

    async def start(self, profile: SandboxProfile, resource: SandboxResource) -> KubernetesSession:
        self._identity(profile, resource)
        profile = self._profiles[resource.id]
        if not profile.kubeconfig or not profile.kube_context or not profile.namespace:
            raise ValueError("Kubernetes profile required")
        api = await self._api(profile)
        while True:
            pod = await self._read(api, profile, resource)
            if not self._safe_pod(profile, pod, self._specs[resource.id]):
                raise ValueError("Sandbox pod specification changed")
            phase = pod.get("status", {}).get("phase")
            ready = any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in pod.get("status", {}).get("conditions", [])
            )
            if phase == "Running" and ready:
                break
            if phase in ("Succeeded", "Failed", "Unknown"):
                raise RuntimeError("Sandbox pod is not running")
            await asyncio.sleep(0.1)

        def open_forwarding() -> Any:
            return _open_bounded_forward(api, profile, resource, self._buffer_limits[resource.id])

        pending = asyncio.create_task(asyncio.to_thread(open_forwarding))
        try:
            forwarding = await asyncio.shield(pending)
        except BaseException:

            def retire(task: asyncio.Task[Any]) -> None:
                if not task.cancelled() and task.exception() is None:
                    with contextlib.suppress(Exception):
                        task.result().close()

            pending.add_done_callback(retire)
            raise
        session = None
        sock = None
        try:
            sock = forwarding.socket(_GUEST_PORT).dup()
            sock.setblocking(False)
            reader, writer = await asyncio.open_connection(sock=sock)

            async def terminal() -> int:
                while True:
                    pod = await self._read(api, profile, resource)
                    status = pod.get("status", {})
                    containers = status.get("containerStatuses") or []
                    if status.get("phase") in ("Succeeded", "Failed") and len(containers) == 1:
                        terminated = containers[0].get("state", {}).get("terminated")
                        if containers[0].get("name") == "runner" and isinstance(terminated, dict):
                            return int(terminated["exitCode"])
                    await asyncio.sleep(0.1)

            session = KubernetesSession(forwarding, reader, writer, self._limits[resource.id], terminal)
            await session.write_frame({"type": "probe"})
            if await session.read_frame() != {"type": "ready"}:
                raise ValueError("Sandbox probe failed")
            observed = await self._read(api, profile, resource)
            if observed.get("status", {}).get("phase") != "Running" or not self._safe_pod(
                profile, observed, self._specs[resource.id]
            ):
                raise ValueError("Sandbox pod specification changed")
            if not await self._verify_isolation(profile):
                raise ValueError("Namespace isolation is not verified")
        except BaseException:
            if session is not None:
                await session.close()
            else:
                if sock is not None:
                    sock.close()
                forwarding.close()
            raise
        self._sessions[resource.id] = session
        return session

    async def stop(self, profile: SandboxProfile, resource: SandboxResource) -> bool:
        try:
            self._identity(profile, resource)
            profile = self._profiles.get(resource.id) or await self._prepared(
                profile, resource.execution_id, release_on_denial=False
            )
            api = await self._api(profile)
            deadline = time.monotonic() + self._terminal_wait
            while True:
                pod = await self._read(api, profile, resource)
                status = pod.get("status", {})
                containers = status.get("containerStatuses") or []
                if (
                    status.get("phase") in ("Succeeded", "Failed")
                    and len(containers) == 1
                    and containers[0].get("name") == "runner"
                    and containers[0].get("state", {}).get("terminated")
                ):
                    break
                if time.monotonic() >= deadline:
                    return False
                await asyncio.sleep(0.2)
            await asyncio.to_thread(
                api.delete_namespaced_pod,
                _name(resource.execution_id),
                profile.namespace,
                body={"preconditions": {"uid": resource.id}},
                _request_timeout=5,
            )
            while True:
                try:
                    await self._read(api, profile, resource)
                except Exception as exc:
                    if getattr(exc, "status", None) == 404:
                        break
                    return False
                if time.monotonic() >= deadline:
                    return False
                await asyncio.sleep(0.2)
            session = self._sessions.pop(resource.id, None)
            if session:
                await session.close()
            self._limits.pop(resource.id, None)
            self._buffer_limits.pop(resource.id, None)
            self._specs.pop(resource.id, None)
            self._profiles.pop(resource.id, None)
            await self._release_profile(profile, resource.execution_id)
            return True
        except (Exception, asyncio.CancelledError):
            return False

    async def reconcile(self, profile: SandboxProfile, resource: SandboxResource) -> bool:
        return await self.stop(profile, resource)
