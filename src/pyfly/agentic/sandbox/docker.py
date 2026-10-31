# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Trusted Docker control-plane adapter. Never expose its daemon to a guest."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from dataclasses import dataclass
from typing import Any

from pyfly.agentic.sandbox.contracts import FunctionInvocation, SandboxProfile, SandboxResource
from pyfly.agentic.sandbox.framing import FrameError, read_frame, write_frame


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class DockerCommand:
    """A bounded, shell-free CLI control client owned by the trusted controller."""

    async def run(self, argv: list[str]) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:

            async def bounded(reader: asyncio.StreamReader) -> bytes:
                data = bytearray()
                while chunk := await reader.read(8192):
                    data.extend(chunk)
                    if len(data) > 1048576:
                        raise ValueError("Control output exceeds limit")
                return bytes(data)

            assert process.stdout is not None and process.stderr is not None
            out, err = await asyncio.gather(bounded(process.stdout), bounded(process.stderr))
            code = await process.wait()
            return CommandResult(code, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()


class DockerSession:
    def __init__(self, process: asyncio.subprocess.Process, limit: int) -> None:
        self.process = process
        self.limit = limit
        self.used = 0
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        count = 0
        while data := await self.process.stderr.read(8192):
            count += len(data)
            if count > self.limit:
                raise FrameError("Guest stderr exceeds limit")

    async def read_frame(self) -> Any:
        assert self.process.stdout is not None
        frame = await read_frame(self.process.stdout, limit=self.limit)
        from pyfly.agentic.sandbox.contracts import encode_json

        self.used += len(encode_json(frame, limit=self.limit))
        if self.used > self.limit:
            raise FrameError("Guest output exceeds limit")
        if self._stderr_task.done():
            self._stderr_task.result()
        return frame

    async def write_frame(self, value: Any) -> None:
        if self.process.stdin is None:
            raise FrameError("Guest input unavailable")
        # The start frame includes source plus input; JSON escaping can expand source bytes.
        await write_frame(self.process.stdin, value, limit=2097152)

    async def wait(self) -> int:
        code = await self.process.wait()
        await self._stderr_task
        assert self.process.stdout is not None
        if await self.process.stdout.read(1):
            raise FrameError("Trailing guest output")
        return code

    async def close(self) -> None:
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()
            await self.process.wait()
        with contextlib.suppress(Exception):
            await self._stderr_task


class DockerSandboxBackend:
    """Create, observe and remove only a pinned, labeled Docker sandbox."""

    def __init__(self, *, command: Any | None = None) -> None:
        self._command = command or DockerCommand()
        self._limits: dict[str, int] = {}
        self._sessions: dict[str, DockerSession] = {}

    @staticmethod
    def _absent(result: CommandResult, resource: SandboxResource) -> bool:
        return result.returncode != 0 and result.stderr.strip() in {
            f"Error: No such object: {resource.id}",
            f"Error response from daemon: No such container: {resource.id}",
        }

    @staticmethod
    def _args(profile: SandboxProfile, *args: str) -> list[str]:
        if profile.kind != "docker" or not profile.docker_context:
            raise ValueError("Docker profile required")
        return ["docker", "--context", profile.docker_context, *args]

    async def _daemon(self, profile: SandboxProfile) -> None:
        result = await self._command.run(self._args(profile, "info", "--format", "{{.ID}}"))
        if result.returncode != 0 or result.stdout.strip() != profile.docker_daemon_id:
            raise ValueError("Docker daemon identity unavailable or mismatched")

    async def create(self, profile: SandboxProfile, invocation: FunctionInvocation) -> SandboxResource:
        await self._daemon(profile)
        image = await self._command.run(
            self._args(profile, "image", "inspect", profile.image, "--format", "{{json .}}")
        )
        if image.returncode != 0:
            raise ValueError("Sandbox image unavailable")
        try:
            data = json.loads(image.stdout)
            image_id = data["Id"]
            config = data["Config"]
            if (
                not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id)
                or config.get("Volumes")
                or config.get("Entrypoint") != ["python", "-I", "-u", "/opt/pyfly/runner.py"]
            ):
                raise ValueError("Unsafe sandbox image")
            if profile.image.startswith("sha256:"):
                if image_id != profile.image:
                    raise ValueError("Sandbox image mismatch")
            elif profile.image not in data.get("RepoDigests", []):
                raise ValueError("Sandbox image digest mismatch")
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid sandbox image metadata") from exc
        limits = invocation.limits
        name = "pyfly-fn-" + invocation.execution_id
        argv = self._args(
            profile,
            "create",
            "--name",
            name,
            "--interactive",
            "--network",
            "none",
            "--log-driver",
            "none",
            "--user",
            "65532:65532",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(limits.max_processes),
            "--memory",
            str(limits.memory_bytes),
            "--memory-swap",
            str(limits.memory_bytes),
            "--cpus",
            "1",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={limits.tmp_bytes}",
            "--label",
            f"pyfly.sandbox.execution={invocation.execution_id}",
            "--label",
            f"pyfly.sandbox.profile={profile.id}",
            "--label",
            f"pyfly.sandbox.revision={profile.revision}",
            profile.image,
        )
        result = await self._command.run(argv)
        identifier = result.stdout.strip()
        if result.returncode != 0 or not re.fullmatch(r"[a-f0-9]{64}", identifier):
            raise RuntimeError("Sandbox creation outcome is unknown")
        self._limits[identifier] = limits.max_output_bytes
        return SandboxResource("docker", invocation.execution_id, identifier, profile.id, profile.revision)

    async def start(self, profile: SandboxProfile, resource: SandboxResource) -> DockerSession:
        if not await self._verify(profile, resource):
            raise ValueError("Sandbox resource no longer exists")
        process = await asyncio.create_subprocess_exec(
            *self._args(profile, "start", "--attach", "--interactive", resource.id),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        session = DockerSession(process, self._limits[resource.id])
        self._sessions[resource.id] = session
        return session

    async def _verify(self, profile: SandboxProfile, resource: SandboxResource) -> bool:
        if (
            resource.kind != "docker"
            or resource.profile_id != profile.id
            or resource.profile_revision != profile.revision
            or not re.fullmatch(r"[a-f0-9]{64}", resource.id)
        ):
            raise ValueError("Sandbox resource identity mismatch")
        await self._daemon(profile)
        result = await self._command.run(
            self._args(profile, "container", "inspect", resource.id, "--format", "{{json .}}")
        )
        if result.returncode != 0:
            if self._absent(result, resource):
                return False
            raise ValueError("Sandbox identity is unavailable")
        data = json.loads(result.stdout)
        labels = data.get("Config", {}).get("Labels") or {}
        if (
            data.get("Id") != resource.id
            or data.get("Config", {}).get("Image") != profile.image
            or labels.get("pyfly.sandbox.execution") != resource.execution_id
            or labels.get("pyfly.sandbox.profile") != profile.id
            or labels.get("pyfly.sandbox.revision") != str(profile.revision)
        ):
            raise ValueError("Sandbox resource identity mismatch")
        return True

    async def stop(self, profile: SandboxProfile, resource: SandboxResource) -> bool:
        try:
            if await self._verify(profile, resource):
                result = await self._command.run(self._args(profile, "container", "rm", "--force", resource.id))
                if result.returncode != 0 or result.stdout.strip() != resource.id:
                    return False
                # Successful removal response is followed by an authoritative no-such-object check.
                await self._daemon(profile)
                observed = await self._command.run(
                    self._args(profile, "container", "inspect", resource.id, "--format", "{{json .}}")
                )
                if not self._absent(observed, resource):
                    return False
            session = self._sessions.pop(resource.id, None)
            if session is not None:
                await session.close()
            self._limits.pop(resource.id, None)
            return True
        except (Exception, asyncio.CancelledError):
            return False

    async def reconcile(self, profile: SandboxProfile, resource: SandboxResource) -> bool:
        return await self.stop(profile, resource)
