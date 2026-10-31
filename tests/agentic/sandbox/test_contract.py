from __future__ import annotations

import hashlib
import math

import pytest

from pyfly.agentic.sandbox import FunctionInvocation, FunctionLimits, SandboxProfile


def invocation(**updates):
    source = "def main(value):\n    return value\n"
    values = {
        "execution_id": "exec-123",
        "source": source,
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "inputs": {"value": 3},
        "limits": FunctionLimits(),
    }
    values.update(updates)
    return FunctionInvocation(**values)


def test_valid_request_is_immutable_and_digest_checked():
    value = invocation()
    assert value.function_name == "main"
    with pytest.raises((AttributeError, TypeError)):
        value.execution_id = "other"
    with pytest.raises(ValueError):
        invocation(source_sha256="0" * 64)
    with pytest.raises(ValueError):
        invocation(source="x" * 65537, source_sha256=hashlib.sha256(b"x" * 65537).hexdigest())


def test_json_and_limits_are_bounded():
    for value in [math.nan, b"not-json", {"nested": {1: "bad"}}]:
        with pytest.raises((ValueError, TypeError)):
            invocation(inputs={"value": value})
    with pytest.raises(ValueError):
        FunctionLimits(timeout_seconds=0)


def test_profile_is_pinned_and_explicit():
    p = SandboxProfile(
        id="local",
        revision=1,
        kind="docker",
        image="sha256:" + "a" * 64,
        docker_context="sandbox",
        docker_daemon_id="daemon-1",
    )
    assert p.kind == "docker"
    with pytest.raises(ValueError):
        SandboxProfile(
            id="local",
            revision=1,
            kind="docker",
            image="python:latest",
            docker_context="sandbox",
            docker_daemon_id="daemon-1",
        )
    with pytest.raises(ValueError):
        SandboxProfile(id="local", revision=1, kind="docker", image="sha256:" + "a" * 64)
