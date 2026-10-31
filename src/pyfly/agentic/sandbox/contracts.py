# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Immutable inputs and explicit outcome states for function sandboxes."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast, runtime_checkable

_JSON_LIMIT = 65536
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_IMAGE = re.compile(r"^(?:sha256:[0-9a-f]{64}|[A-Za-z0-9][A-Za-z0-9._/:@-]*@sha256:[0-9a-f]{64})$")


class PrelaunchRejected(ValueError):
    """A policy rejection known to have occurred before resource creation."""


def _json_value(value: Any, depth: int = 0) -> None:
    if depth > 32:
        raise ValueError("JSON nesting exceeds the limit")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return
    if isinstance(value, list):
        for child in value:
            _json_value(child, depth + 1)
        return
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        for child in value.values():
            _json_value(child, depth + 1)
        return
    raise TypeError("Value must be JSON")


def encode_json(value: Any, *, limit: int = _JSON_LIMIT) -> bytes:
    _json_value(value)
    try:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (UnicodeEncodeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid JSON value") from exc
    if len(data) > limit:
        raise ValueError("JSON exceeds the size limit")
    return data


@dataclass(frozen=True, slots=True)
class FunctionLimits:
    timeout_seconds: float = 10.0
    memory_bytes: int = 134217728
    cpu_seconds: int = 5
    max_processes: int = 16
    max_output_bytes: int = 65536
    max_frames: int = 64
    max_input_bytes: int = 65536
    tmp_bytes: int = 16777216

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 300
        ):
            raise ValueError("Invalid sandbox timeout")
        for name, low, high in [
            ("memory_bytes", 16777216, 1073741824),
            ("cpu_seconds", 1, 300),
            ("max_processes", 1, 128),
            ("max_output_bytes", 1, 1048576),
            ("max_frames", 1, 1024),
            ("max_input_bytes", 1, 1048576),
            ("tmp_bytes", 1, 268435456),
        ]:
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"Invalid sandbox {name}")


@dataclass(frozen=True, slots=True, init=False)
class FunctionInvocation:
    execution_id: str
    source: str
    source_sha256: str
    function_name: str
    limits: FunctionLimits
    _inputs_json: bytes

    def __init__(
        self,
        execution_id: str,
        source: str,
        source_sha256: str,
        inputs: dict[str, Any],
        limits: FunctionLimits,
        function_name: str = "main",
    ) -> None:
        if not isinstance(execution_id, str) or not _NAME.fullmatch(execution_id):
            raise ValueError("Invalid execution ID")
        if not isinstance(source, str):
            raise TypeError("Source must be UTF-8 text")
        try:
            source_bytes = source.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("Source must be UTF-8 text") from exc
        if not source_bytes or len(source_bytes) > 65536 or hashlib.sha256(source_bytes).hexdigest() != source_sha256:
            raise ValueError("Source digest or size is invalid")
        if not isinstance(function_name, str) or not function_name.isidentifier() or function_name.startswith("_"):
            raise ValueError("Invalid function name")
        if not isinstance(inputs, dict) or not isinstance(limits, FunctionLimits):
            raise TypeError("Inputs must be an object and limits must be FunctionLimits")
        data = encode_json(inputs, limit=min(limits.max_input_bytes, _JSON_LIMIT))
        for key, value in [
            ("execution_id", execution_id),
            ("source", source),
            ("source_sha256", source_sha256),
            ("inputs_json", data),
            ("limits", limits),
            ("function_name", function_name),
        ]:
            object.__setattr__(self, "_inputs_json" if key == "inputs_json" else key, value)

    @property
    def inputs(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self._inputs_json))


@dataclass(frozen=True, slots=True)
class SandboxProfile:
    id: str
    revision: int
    kind: Literal["docker", "kubernetes"]
    image: str
    docker_context: str | None = None
    docker_daemon_id: str | None = None
    kubeconfig: str | None = None
    kube_context: str | None = None
    namespace: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.id, str)
            or not _NAME.fullmatch(self.id)
            or type(self.revision) is not int
            or self.revision < 1
        ):
            raise ValueError("Invalid sandbox profile identity")
        if not isinstance(self.image, str) or not _IMAGE.fullmatch(self.image):
            raise ValueError("Sandbox image must be pinned by SHA-256 digest")
        if self.kind == "docker":
            if (
                not self.docker_context
                or not self.docker_daemon_id
                or any((self.kubeconfig, self.kube_context, self.namespace))
            ):
                raise ValueError("Docker sandbox requires a pinned context and daemon")
        elif self.kind == "kubernetes":
            if (
                not self.kubeconfig
                or not self.kube_context
                or not self.namespace
                or any((self.docker_context, self.docker_daemon_id))
                or self.image.startswith("sha256:")
            ):
                raise ValueError("Kubernetes sandbox requires an explicit context, namespace and registry image digest")
        else:
            raise ValueError("Unknown sandbox target")
        for value in (self.docker_context, self.docker_daemon_id, self.kubeconfig, self.kube_context, self.namespace):
            if value is not None and (
                not isinstance(value, str) or len(value) > 512 or any(ord(c) < 32 for c in value)
            ):
                raise ValueError("Invalid sandbox target")


@dataclass(frozen=True, slots=True)
class SandboxResource:
    kind: Literal["docker", "kubernetes"]
    execution_id: str
    id: str
    profile_id: str
    profile_revision: int


@dataclass(frozen=True, slots=True)
class FunctionOutcome:
    status: Literal["succeeded", "failed", "timed_out", "cancelled", "unknown"]
    cleanup_confirmed: bool
    output: Any = None
    error_code: str | None = None
    resource: SandboxResource | None = None
    elapsed_ms: int = 0


EffectHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]
ResourceObserver = Callable[[SandboxResource], Awaitable[None]]
Authorizer = Callable[[], Awaitable[None]]


@runtime_checkable
class FunctionSandboxPort(Protocol):
    async def invoke(
        self,
        profile: SandboxProfile,
        invocation: FunctionInvocation,
        *,
        effects: EffectHandler | None,
        observe: ResourceObserver,
        authorize: Authorizer,
    ) -> FunctionOutcome: ...
    async def reconcile(self, profile: SandboxProfile, resource: SandboxResource) -> bool: ...
