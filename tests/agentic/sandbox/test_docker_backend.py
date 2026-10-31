from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from pyfly.agentic.sandbox import FunctionInvocation, FunctionLimits, SandboxProfile
from pyfly.agentic.sandbox.docker import DockerSandboxBackend


def request():
    code = "def main(inputs):\n    return inputs\n"
    return FunctionInvocation("exec-1", code, hashlib.sha256(code.encode()).hexdigest(), {}, FunctionLimits())


def profile():
    return SandboxProfile(
        "trusted", 1, "docker", "sha256:" + "a" * 64, docker_context="safe", docker_daemon_id="daemon"
    )


class Command:
    def __init__(self):
        self.commands = []

    async def run(self, argv):
        self.commands.append(argv)
        if "info" in argv:
            return SimpleNamespace(returncode=0, stdout="daemon\n", stderr="")
        if "image" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "Id": "sha256:" + "a" * 64,
                        "Config": {"Volumes": None, "Entrypoint": ["python", "-I", "-u", "/opt/pyfly/runner.py"]},
                    }
                ),
                stderr="",
            )
        if "create" in argv:
            return SimpleNamespace(returncode=0, stdout="b" * 64 + "\n", stderr="")
        raise AssertionError(argv)


@pytest.mark.asyncio
async def test_docker_create_is_pinned_networkless_and_resource_bounded():
    command = Command()
    backend = DockerSandboxBackend(command=command)
    resource = await backend.create(profile(), request())
    assert resource.id == "b" * 64
    argv = command.commands[-1]
    assert argv[:3] == ["docker", "--context", "safe"]
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--user") + 1] == "65532:65532"
    assert "--read-only" in argv and "--cap-drop" in argv and "--pids-limit" in argv
    assert argv[argv.index("--log-driver") + 1] == "none"
    assert "--privileged" not in argv and "--volume" not in argv and "--env" not in argv


@pytest.mark.asyncio
async def test_wrong_daemon_fails_before_create():
    command = Command()
    backend = DockerSandboxBackend(command=command)
    p = SandboxProfile("trusted", 1, "docker", "sha256:" + "a" * 64, docker_context="safe", docker_daemon_id="other")
    with pytest.raises(ValueError):
        await backend.create(p, request())
    assert not any("create" in x for x in command.commands)


@pytest.mark.asyncio
async def test_reconcile_confirms_absence_only_on_matching_daemon():
    from pyfly.agentic.sandbox import SandboxResource

    class Gone(Command):
        async def run(self, argv):
            if "inspect" in argv and "container" in argv:
                return SimpleNamespace(returncode=1, stdout="", stderr="Error: No such object: " + "b" * 64)
            return await super().run(argv)

    resource = SandboxResource("docker", "exec-1", "b" * 64, "trusted", 1)
    backend = DockerSandboxBackend(command=Gone())
    assert await backend.reconcile(profile(), resource) is True
    other = SandboxProfile(
        "trusted", 1, "docker", "sha256:" + "a" * 64, docker_context="safe", docker_daemon_id="other"
    )
    assert await backend.reconcile(other, resource) is False


@pytest.mark.asyncio
async def test_reconcile_recognizes_docker_daemon_no_such_container():
    from pyfly.agentic.sandbox import SandboxResource

    class Gone(Command):
        async def run(self, argv):
            if "inspect" in argv and "container" in argv:
                return SimpleNamespace(
                    returncode=1, stdout="", stderr="Error response from daemon: No such container: " + "b" * 64
                )
            return await super().run(argv)

    resource = SandboxResource("docker", "exec-1", "b" * 64, "trusted", 1)
    assert await DockerSandboxBackend(command=Gone()).reconcile(profile(), resource)


@pytest.mark.asyncio
async def test_confirmed_absent_container_retires_local_session():
    from pyfly.agentic.sandbox import SandboxResource

    class Gone(Command):
        async def run(self, argv):
            if "container" in argv and "inspect" in argv:
                return SimpleNamespace(returncode=1, stdout="", stderr="Error: No such object: " + "b" * 64)
            return await super().run(argv)

    class Process:
        returncode = None
        killed = False

        def kill(self):
            self.killed = True

        async def wait(self):
            self.returncode = -9

    process = Process()
    backend = DockerSandboxBackend(command=Gone())
    backend._limits["b" * 64] = 10

    async def close():
        process.kill()
        await process.wait()

    backend._sessions["b" * 64] = SimpleNamespace(process=process, close=close)
    resource = SandboxResource("docker", "exec-1", "b" * 64, "trusted", 1)
    assert await backend.reconcile(profile(), resource)
    assert process.killed and not backend._sessions and not backend._limits
