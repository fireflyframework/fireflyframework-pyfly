# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Container-only standard-library bootstrap. The OCI boundary supplies isolation."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import io
import json
import os
import resource
import socket
import struct
import sys

_MAX_FRAME = 2097152
_in = sys.stdin.buffer
_out = sys.stdout.buffer


def _read() -> object:
    header = _in.read(4)
    if len(header) != 4:
        raise ValueError("invalid frame")
    size = struct.unpack(">I", header)[0]
    if not 0 < size <= _MAX_FRAME:
        raise ValueError("invalid frame")
    data = _in.read(size)
    if len(data) != size:
        raise ValueError("invalid frame")
    return json.loads(data)


def _write(value: object, limit: int) -> None:
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > limit:
        raise ValueError("frame exceeds limit")
    _out.write(struct.pack(">I", len(data)) + data)
    _out.flush()


class _Discard(io.TextIOBase):
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._used = 0

    def write(self, text: str) -> int:
        self._used += len(text.encode("utf-8"))
        if self._used > self._limit:
            raise ValueError("output limit")
        return len(text)


def _limit(name: int, value: int) -> None:
    current = resource.getrlimit(name)
    hard = current[1]
    ceiling = value if hard == resource.RLIM_INFINITY else min(value, hard)
    resource.setrlimit(name, (ceiling, ceiling))


def main() -> None:
    start = _read()
    if start == {"type": "probe"}:
        _write({"type": "ready"}, 1024)
        start = _read()
    if not isinstance(start, dict) or start.get("type") != "start":
        return
    source, digest, function, inputs, limits = (
        start.get(k) for k in ("source", "source_sha256", "function_name", "inputs", "limits")
    )
    if (
        not isinstance(source, str)
        or not isinstance(digest, str)
        or hashlib.sha256(source.encode("utf-8")).hexdigest() != digest
        or not isinstance(function, str)
        or not isinstance(inputs, dict)
        or not isinstance(limits, dict)
    ):
        return
    output_limit = int(limits["max_output_bytes"])
    if sys.platform.startswith("linux"):
        _limit(resource.RLIMIT_CPU, int(limits["cpu_seconds"]))
        _limit(resource.RLIMIT_AS, int(limits["memory_bytes"]))
        _limit(resource.RLIMIT_NPROC, int(limits["max_processes"]))
        _limit(resource.RLIMIT_CORE, 0)
    next_id = 0

    def effect(name: str, args: dict[str, object]) -> object:
        nonlocal next_id
        if not isinstance(name, str) or not isinstance(args, dict):
            raise ValueError("invalid effect")
        next_id += 1
        _write({"type": "effect", "id": next_id, "name": name, "args": args}, output_limit)
        reply = _read()
        if (
            not isinstance(reply, dict)
            or set(reply) != {"type", "id", "value"}
            or reply["type"] != "effect_result"
            or reply["id"] != next_id
        ):
            raise ValueError("invalid effect response")
        return reply["value"]

    sys.stdout = _Discard(output_limit)
    sys.stderr = _Discard(output_limit)
    try:
        namespace = {"__name__": "__pyfly_function__", "effect": effect}
        exec(compile(source, "<sandbox>", "exec"), namespace)
        target = namespace.get(function)
        if not callable(target):
            raise ValueError("missing function")
        result = target(inputs)
        if inspect.isawaitable(result):

            async def await_result() -> object:
                return await result

            result = asyncio.run(await_result())
        _write({"type": "result", "output": result}, output_limit)
    except BaseException:
        try:
            _write({"type": "error", "code": "GUEST_ERROR"}, output_limit)
        except BaseException:
            os._exit(1)


if __name__ == "__main__":
    if len(sys.argv) in (3, 5) and sys.argv[1] == "--socket":
        port = int(sys.argv[2])
        if not 1 <= port <= 65535 or (len(sys.argv) == 5 and sys.argv[3:] != ["--ready-file", "/tmp/pyfly-ready"]):
            os._exit(1)
        null_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null_fd, 1)
        os.dup2(null_fd, 2)
        if null_fd > 2:
            os.close(null_fd)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", port))
            listener.listen(1)
            if len(sys.argv) == 5:
                ready = os.open("/tmp/pyfly-ready", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(ready)
            connection, _ = listener.accept()
        with connection:
            _in = connection.makefile("rb")
            _out = connection.makefile("wb")
            main()
        os._exit(0)
    main()
