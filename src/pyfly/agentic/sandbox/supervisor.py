# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Bounded supervision of an isolated backend and its untrusted guest frames."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any, Literal, Protocol

from pyfly.agentic.sandbox.contracts import (
    Authorizer,
    EffectHandler,
    FunctionInvocation,
    FunctionOutcome,
    PrelaunchRejected,
    ResourceObserver,
    SandboxProfile,
    SandboxResource,
    encode_json,
)


class SandboxSession(Protocol):
    async def read_frame(self) -> Any: ...
    async def write_frame(self, value: Any) -> None: ...
    async def wait(self) -> int: ...


class SandboxBackend(Protocol):
    async def create(self, profile: SandboxProfile, invocation: FunctionInvocation) -> SandboxResource: ...
    async def start(self, profile: SandboxProfile, resource: SandboxResource) -> SandboxSession: ...
    async def stop(self, profile: SandboxProfile, resource: SandboxResource) -> bool: ...
    async def reconcile(self, profile: SandboxProfile, resource: SandboxResource) -> bool: ...


class RoutingSandboxBackend:
    """Select an explicitly configured trusted backend by approved profile kind."""

    def __init__(self, backends: Mapping[str, SandboxBackend]) -> None:
        self._backends = dict(backends)

    def _select(self, profile: SandboxProfile) -> SandboxBackend:
        try:
            return self._backends[profile.kind]
        except KeyError as exc:
            raise ValueError("Sandbox backend is not configured") from exc

    async def create(self, profile: SandboxProfile, invocation: FunctionInvocation) -> SandboxResource:
        return await self._select(profile).create(profile, invocation)

    async def start(self, profile: SandboxProfile, resource: SandboxResource) -> SandboxSession:
        return await self._select(profile).start(profile, resource)

    async def stop(self, profile: SandboxProfile, resource: SandboxResource) -> bool:
        return await self._select(profile).stop(profile, resource)

    async def reconcile(self, profile: SandboxProfile, resource: SandboxResource) -> bool:
        return await self._select(profile).reconcile(profile, resource)


class ProtocolError(Exception):
    pass


class EffectUnknownError(Exception):
    pass


class _PolicyRejected(Exception):
    pass


_PRELAUNCH_REJECTED = object()


def _retire(task: asyncio.Future[Any]) -> None:
    if not task.done():
        task.cancel()
        task.add_done_callback(lambda result: None if result.cancelled() else result.exception())


async def _controlled(awaitable: Any) -> Any:
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    finally:
        _retire(task)


async def _supervised(awaitable: Any, authorize: Authorizer, *, effect: bool = False) -> Any:
    task = asyncio.ensure_future(awaitable)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=0.25)
            if done:
                return task.result()
            await _controlled(authorize())
    except asyncio.CancelledError as exc:
        if effect:
            raise EffectUnknownError from exc
        raise
    finally:
        _retire(task)


class FunctionSandbox:
    """Compose a trusted backend with authorization and frame supervision.

    The backend must enforce isolation and exact-resource cleanup; this class does
    not make an arbitrary backend safe. Effects are trusted host callbacks.
    """

    def __init__(self, backend: SandboxBackend, *, cleanup_timeout: float = 5.0) -> None:
        if not 0 < cleanup_timeout <= 60:
            raise ValueError("Invalid cleanup timeout")
        self._backend = backend
        self._cleanup_timeout = cleanup_timeout

    async def invoke(
        self,
        profile: SandboxProfile,
        invocation: FunctionInvocation,
        *,
        effects: EffectHandler | None,
        observe: ResourceObserver,
        authorize: Authorizer,
    ) -> FunctionOutcome:
        started = time.monotonic()
        resource = None
        clean = True
        status: Literal["succeeded", "failed", "timed_out", "cancelled", "unknown"] = "failed"
        code: str | None = "SANDBOX_ERROR"
        output = None
        creating = False
        try:
            async with asyncio.timeout(invocation.limits.timeout_seconds):
                await _controlled(authorize())
                creating = True

                async def create_resource() -> Any:
                    try:
                        return await self._backend.create(profile, invocation)
                    except PrelaunchRejected:
                        return _PRELAUNCH_REJECTED

                created = await _supervised(create_resource(), authorize)
                if created is _PRELAUNCH_REJECTED:
                    creating = False
                    raise _PolicyRejected
                resource = created
                creating = False
                clean = False
                if (resource.execution_id, resource.profile_id, resource.profile_revision, resource.kind) != (
                    invocation.execution_id,
                    profile.id,
                    profile.revision,
                    profile.kind,
                ):
                    resource = None
                    raise ProtocolError
                await _controlled(observe(resource))
                await _controlled(authorize())
                session = await _supervised(self._backend.start(profile, resource), authorize)
                await _controlled(authorize())
                await _controlled(
                    session.write_frame(
                        {
                            "type": "start",
                            "source": invocation.source,
                            "source_sha256": invocation.source_sha256,
                            "function_name": invocation.function_name,
                            "inputs": invocation.inputs,
                            "limits": {
                                "cpu_seconds": invocation.limits.cpu_seconds,
                                "memory_bytes": invocation.limits.memory_bytes,
                                "max_processes": invocation.limits.max_processes,
                                "max_output_bytes": invocation.limits.max_output_bytes,
                            },
                        }
                    )
                )
                ids: set[int] = set()
                total_guest_bytes = 0
                for _ in range(invocation.limits.max_frames):
                    frame = await _supervised(session.read_frame(), authorize)
                    total_guest_bytes += len(encode_json(frame, limit=invocation.limits.max_output_bytes))
                    if total_guest_bytes > invocation.limits.max_output_bytes:
                        raise ProtocolError
                    if not isinstance(frame, dict):
                        raise ProtocolError
                    if frame.get("type") == "result" and set(frame) == {"type", "output"}:
                        output = frame["output"]
                        if await _supervised(session.wait(), authorize) != 0:
                            raise ProtocolError
                        status, code = "succeeded", None
                        break
                    if (
                        frame.get("type") == "error"
                        and set(frame) == {"type", "code"}
                        and frame["code"] == "GUEST_ERROR"
                    ):
                        if await _supervised(session.wait(), authorize) != 0:
                            raise ProtocolError
                        status, code = "failed", "GUEST_ERROR"
                        break
                    if frame.get("type") != "effect" or set(frame) != {"type", "id", "name", "args"}:
                        raise ProtocolError
                    effect_id, name, args = frame["id"], frame["name"], frame["args"]
                    if (
                        type(effect_id) is not int
                        or effect_id < 1
                        or effect_id in ids
                        or not isinstance(name, str)
                        or not name
                        or len(name) > 128
                        or not isinstance(args, dict)
                        or effects is None
                    ):
                        raise ProtocolError
                    ids.add(effect_id)
                    await _controlled(authorize())
                    try:
                        result = await _supervised(effects(name, args), authorize, effect=True)
                    except Exception as exc:
                        raise EffectUnknownError from exc
                    encode_json(result, limit=invocation.limits.max_output_bytes)
                    await _controlled(authorize())
                    await _controlled(session.write_frame({"type": "effect_result", "id": effect_id, "value": result}))
                else:
                    raise ProtocolError
        except asyncio.CancelledError:
            status, code = "cancelled", "CANCELLED"
        except TimeoutError:
            status, code = "timed_out", "TIMEOUT"
        except EffectUnknownError:
            status, code = "unknown", "EFFECT_UNKNOWN"
        except _PolicyRejected:
            status, code, clean = "failed", "POLICY_REJECTED", True
        except ProtocolError:
            status, code = "failed", "PROTOCOL_ERROR"
        except Exception:
            status, code = ("unknown", "CREATE_UNKNOWN") if creating else ("failed", "SANDBOX_ERROR")
        if resource is not None:
            try:
                clean = await asyncio.wait_for(
                    asyncio.shield(self._backend.stop(profile, resource)), self._cleanup_timeout
                )
            except BaseException:
                clean = False
            if not clean:
                status, code = "unknown", "CLEANUP_UNCONFIRMED"
                output = None
        elif creating:
            status, code, clean = "unknown", "CREATE_UNKNOWN", False
        elif not clean:
            status, code, output = "unknown", "CLEANUP_UNCONFIRMED", None
        if status != "succeeded":
            output = None
        return FunctionOutcome(
            status=status,
            cleanup_confirmed=clean,
            output=output,
            error_code=code,
            resource=resource,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    async def reconcile(self, profile: SandboxProfile, resource: SandboxResource) -> bool:
        if (resource.kind, resource.profile_id, resource.profile_revision) != (
            profile.kind,
            profile.id,
            profile.revision,
        ):
            return False
        return await self._backend.reconcile(profile, resource)
