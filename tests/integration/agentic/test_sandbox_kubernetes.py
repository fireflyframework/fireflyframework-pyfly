# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Opt-in checks in an independently CNI-qualified, dedicated namespace."""

from __future__ import annotations

import hashlib
import os
import uuid

import pytest

from pyfly.agentic.sandbox import (
    FunctionInvocation,
    FunctionLimits,
    FunctionSandbox,
    KubernetesSandboxBackend,
    SandboxProfile,
)

pytestmark = [pytest.mark.integration]


async def prepare(profile, execution_id):
    # Test-only: the qualification kubeconfig is already held in an owned directory.
    return profile


async def release(profile, execution_id):
    pass


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
        ('def main(inputs):\n import os\n os.write(1, b"forged-output")\n return 1\n', "succeeded", 1),
        (
            "def main(inputs):\n import subprocess,sys\n"
            ' p=subprocess.run([sys.executable,"-c","import os;os.write(1,b\'Q\'*262144);'
            'os.write(2,b\'R\'*262144)"],timeout=3)\n return {"child":p.returncode}\n',
            "succeeded",
            {"child": 0},
        ),
        ("def main(inputs):\n while True: pass\n", "timed_out", None),
    ],
)
async def test_real_kubernetes_sandbox(source, expected, output):
    config = os.getenv("PYFLY_SANDBOX_TEST_KUBECONFIG")
    context = os.getenv("PYFLY_SANDBOX_TEST_KUBE_CONTEXT")
    namespace = os.getenv("PYFLY_SANDBOX_TEST_NAMESPACE")
    image = os.getenv("PYFLY_SANDBOX_TEST_KUBE_IMAGE")
    if not all((config, context, namespace, image)):
        pytest.skip("Explicit CNI-qualified Kubernetes sandbox profile required")
    profile = SandboxProfile(
        "sandbox", 1, "kubernetes", image, kubeconfig=config, kube_context=context, namespace=namespace
    )
    invocation = FunctionInvocation(
        "q" + uuid.uuid4().hex,
        source,
        hashlib.sha256(source.encode()).hexdigest(),
        {"v": 4},
        FunctionLimits(timeout_seconds=8, cpu_seconds=15, max_processes=64),
    )
    seen = []

    async def verify_isolation(profile):
        # The caller supplies this only after separately validating the namespace/CNI policy.
        return True

    async def authorize():
        pass

    async def observe(resource):
        seen.append(resource)

    async def effects(name, args):
        assert name == "echo"
        return args

    outcome = await FunctionSandbox(
        KubernetesSandboxBackend(prepare_profile=prepare, release_profile=release, verify_isolation=verify_isolation),
        cleanup_timeout=55,
    ).invoke(profile, invocation, effects=effects, observe=observe, authorize=authorize)
    assert len(seen) == 1 and outcome.resource == seen[0]
    assert outcome.status == expected and outcome.output == output, outcome
    assert outcome.cleanup_confirmed


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
async def test_kubernetes_process_and_thread_limits(source):
    config = os.getenv("PYFLY_SANDBOX_TEST_KUBECONFIG")
    context = os.getenv("PYFLY_SANDBOX_TEST_KUBE_CONTEXT")
    namespace = os.getenv("PYFLY_SANDBOX_TEST_NAMESPACE")
    image = os.getenv("PYFLY_SANDBOX_TEST_KUBE_IMAGE")
    if not all((config, context, namespace, image)):
        pytest.skip("Explicit CNI-qualified Kubernetes sandbox profile required")
    profile = SandboxProfile(
        "sandbox", 1, "kubernetes", image, kubeconfig=config, kube_context=context, namespace=namespace
    )
    invocation = FunctionInvocation(
        "q" + uuid.uuid4().hex,
        source,
        hashlib.sha256(source.encode()).hexdigest(),
        {},
        FunctionLimits(timeout_seconds=10, max_processes=8),
    )

    async def verify(profile):
        return True

    async def noop(*args):
        pass

    outcome = await FunctionSandbox(
        KubernetesSandboxBackend(prepare_profile=prepare, release_profile=release, verify_isolation=verify),
        cleanup_timeout=55,
    ).invoke(profile, invocation, effects=None, observe=noop, authorize=noop)
    assert outcome.status == "succeeded" and outcome.cleanup_confirmed
    assert 0 <= outcome.output["count"] < 8


@pytest.mark.asyncio
async def test_same_name_replacement_denies_source_delivery():
    import asyncio

    config = os.getenv("PYFLY_SANDBOX_TEST_KUBECONFIG")
    context = os.getenv("PYFLY_SANDBOX_TEST_KUBE_CONTEXT")
    namespace = os.getenv("PYFLY_SANDBOX_TEST_NAMESPACE")
    image = os.getenv("PYFLY_SANDBOX_TEST_KUBE_IMAGE")
    if not all((config, context, namespace, image)):
        pytest.skip("Explicit CNI-qualified Kubernetes sandbox profile required")
    profile = SandboxProfile(
        "sandbox", 1, "kubernetes", image, kubeconfig=config, kube_context=context, namespace=namespace
    )
    source = 'def main(inputs):\n return effect("must_not_run", {})\n'
    invocation = FunctionInvocation(
        "q" + uuid.uuid4().hex,
        source,
        hashlib.sha256(source.encode()).hexdigest(),
        {},
        FunctionLimits(timeout_seconds=12),
    )
    replacement = []
    effects_seen = []

    async def verify(profile):
        return True

    class ReplacingBackend(KubernetesSandboxBackend):
        async def _read(self, api, profile, resource):
            if getattr(self, "probe_ready", False) and not replacement:
                self.probe_ready = False
                name = "pyfly-fn-" + hashlib.sha256(resource.execution_id.encode()).hexdigest()[:40]
                await asyncio.to_thread(
                    api.delete_namespaced_pod,
                    name,
                    profile.namespace,
                    body={"preconditions": {"uid": resource.id}, "gracePeriodSeconds": 1},
                    _request_timeout=5,
                )
                for _ in range(100):
                    try:
                        await asyncio.to_thread(api.read_namespaced_pod, name, profile.namespace, _request_timeout=5)
                    except Exception as exc:
                        if getattr(exc, "status", None) == 404:
                            break
                        raise
                    await asyncio.sleep(0.1)
                else:
                    raise RuntimeError("Original pod did not terminate")
                replacement.append(await self.create(profile, invocation))
            return await super()._read(api, profile, resource)

        async def start(self, profile, resource):
            # The first read occurs before attaching; the next occurs after READY.
            original_read = self._read

            async def intercept(api, profile, resource):
                result = await original_read(api, profile, resource)
                status = result.get("status", {})
                if status.get("phase") == "Running" and any(
                    c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", [])
                ):
                    self.probe_ready = True
                return result

            self._read = intercept
            return await super().start(profile, resource)

    backend = ReplacingBackend(prepare_profile=prepare, release_profile=release, verify_isolation=verify)

    async def noop(*args):
        pass

    async def effects(name, args):
        effects_seen.append(name)

    try:
        outcome = await FunctionSandbox(backend, cleanup_timeout=55).invoke(
            profile, invocation, effects=effects, observe=noop, authorize=noop
        )
        assert replacement and replacement[0].id != outcome.resource.id
        assert outcome.status == "unknown" and not outcome.cleanup_confirmed
        assert not effects_seen
    finally:
        if replacement:
            backend.probe_ready = False
            assert await backend.stop(profile, replacement[0]), replacement[0]
