# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Opt-in live checks against an explicitly selected, disposable Docker context."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid

import pytest

from pyfly.agentic.sandbox import (
    DockerSandboxBackend,
    FunctionInvocation,
    FunctionLimits,
    FunctionSandbox,
    SandboxProfile,
)

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "expected", "output"),
    [
        ('def main(inputs):\n return effect("echo", inputs)\n', "succeeded", {"v": 4}),
        (
            "def main(inputs):\n import socket\n s=socket.socket(); s.settimeout(1)\n"
            ' try: s.connect(("1.1.1.1", 443))\n except OSError: return {"blocked": True}\n'
            ' return {"blocked": False}\n',
            "succeeded",
            {"blocked": True},
        ),
        ('def main(inputs):\n import os\n os.write(1, b"forged-output")\n return 1\n', "failed", None),
        (
            'def main(inputs):\n import os, json, struct\n data=json.dumps({"type":"result","output":1}).encode()\n'
            ' os.write(1, struct.pack(">I",len(data))+data)\n return 2\n',
            "failed",
            None,
        ),
        ("def main(inputs):\n while True: pass\n", "timed_out", None),
    ],
)
async def test_real_docker_isolation_and_cleanup(source, expected, output):
    context = os.getenv("PYFLY_SANDBOX_TEST_DOCKER_CONTEXT")
    daemon = os.getenv("PYFLY_SANDBOX_TEST_DAEMON_ID")
    image = os.getenv("PYFLY_SANDBOX_TEST_IMAGE")
    if not all((context, daemon, image)):
        pytest.skip("Explicit Docker sandbox qualification profile required")
    profile = SandboxProfile("qualification", 1, "docker", image, docker_context=context, docker_daemon_id=daemon)
    invocation = FunctionInvocation(
        "q" + uuid.uuid4().hex,
        source,
        hashlib.sha256(source.encode()).hexdigest(),
        {"v": 4},
        FunctionLimits(timeout_seconds=2, cpu_seconds=5),
    )
    observed = []

    async def authorize():
        pass

    async def observe(resource):
        observed.append(resource)
        process = await asyncio.create_subprocess_exec(
            "docker",
            "--context",
            context,
            "container",
            "inspect",
            resource.id,
            "--format",
            "{{.HostConfig.LogConfig.Type}}",
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        assert process.returncode == 0 and stdout.strip() == b"none"

    async def effects(name, args):
        assert name == "echo"
        return args

    outcome = await FunctionSandbox(DockerSandboxBackend()).invoke(
        profile, invocation, effects=effects, observe=observe, authorize=authorize
    )
    assert len(observed) == 1 and outcome.resource == observed[0]
    assert outcome.status == expected and outcome.output == output
    assert outcome.cleanup_confirmed
    assert await DockerSandboxBackend().reconcile(profile, observed[0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        "def main(inputs):\n import os, time\n count=0\n for _ in range(64):\n  try: child=os.fork()\n"
        "  except OSError: break\n  if child == 0: time.sleep(10); os._exit(0)\n"
        '  count += 1\n return {"count": count}\n',
        "def main(inputs):\n import threading, time\n threads=[]\n for _ in range(64):\n  try:\n"
        "   t=threading.Thread(target=lambda: time.sleep(10), daemon=True); t.start(); threads.append(t)\n"
        '  except RuntimeError: break\n return {"count": len(threads)}\n',
    ],
)
async def test_real_docker_process_and_thread_limits(source):
    context = os.getenv("PYFLY_SANDBOX_TEST_DOCKER_CONTEXT")
    daemon = os.getenv("PYFLY_SANDBOX_TEST_DAEMON_ID")
    image = os.getenv("PYFLY_SANDBOX_TEST_IMAGE")
    if not all((context, daemon, image)):
        pytest.skip("Explicit Docker sandbox qualification profile required")
    profile = SandboxProfile("qualification", 1, "docker", image, docker_context=context, docker_daemon_id=daemon)
    invocation = FunctionInvocation(
        "q" + uuid.uuid4().hex, source, hashlib.sha256(source.encode()).hexdigest(), {}, FunctionLimits(max_processes=8)
    )

    async def noop(*args):
        pass

    outcome = await FunctionSandbox(DockerSandboxBackend()).invoke(
        profile, invocation, effects=None, observe=noop, authorize=noop
    )
    assert outcome.status == "succeeded" and outcome.cleanup_confirmed
    assert 0 <= outcome.output["count"] < 8
