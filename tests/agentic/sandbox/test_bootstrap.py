from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import sys
from pathlib import Path

import pytest

BOOTSTRAP = Path(__file__).parents[3] / "src/pyfly/agentic/sandbox/runner.py"


def frame(value):
    raw = json.dumps(value, separators=(",", ":")).encode()
    return struct.pack(">I", len(raw)) + raw


async def receive(reader):
    size = struct.unpack(">I", await reader.readexactly(4))[0]
    return json.loads(await reader.readexactly(size))


@pytest.mark.asyncio
async def test_guest_acknowledges_session_probe_before_source():
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(BOOTSTRAP),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    process.stdin.write(frame({"type": "probe"}))
    await process.stdin.drain()
    assert await asyncio.wait_for(receive(process.stdout), 3) == {"type": "ready"}
    process.stdin.close()
    await process.wait()


@pytest.mark.asyncio
async def test_guest_main_and_effect_exchange():
    source = 'def main(inputs):\n    return {"result": effect("echo", inputs)}\n'
    payload = {
        "type": "start",
        "source": source,
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "function_name": "main",
        "inputs": {"x": 3},
        "limits": {"cpu_seconds": 5, "memory_bytes": 134217728, "max_processes": 16, "max_output_bytes": 65536},
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(BOOTSTRAP),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    process.stdin.write(frame(payload))
    await process.stdin.drain()
    effect = await asyncio.wait_for(receive(process.stdout), 3)
    assert effect == {"type": "effect", "id": 1, "name": "echo", "args": {"x": 3}}
    process.stdin.write(frame({"type": "effect_result", "id": 1, "value": {"answer": 4}}))
    await process.stdin.drain()
    assert await asyncio.wait_for(receive(process.stdout), 3) == {"type": "result", "output": {"result": {"answer": 4}}}
    assert await process.wait() == 0


@pytest.mark.asyncio
async def test_guest_exception_is_not_leaked():
    source = 'def main(inputs):\n    raise ValueError("SECRET-MARKER")\n'
    payload = {
        "type": "start",
        "source": source,
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "function_name": "main",
        "inputs": {},
        "limits": {"cpu_seconds": 5, "memory_bytes": 134217728, "max_processes": 16, "max_output_bytes": 65536},
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(BOOTSTRAP),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    process.stdin.write(frame(payload))
    await process.stdin.drain()
    assert await asyncio.wait_for(receive(process.stdout), 3) == {"type": "error", "code": "GUEST_ERROR"}
    assert b"SECRET-MARKER" not in await process.stderr.read()
    assert await process.wait() == 0


@pytest.mark.asyncio
async def test_guest_custom_awaitable_result():
    source = (
        "class Value:\n"
        " def __await__(self):\n"
        "  async def value(): return 7\n"
        "  return value().__await__()\n"
        "def main(inputs): return Value()\n"
    )
    payload = {
        "type": "start",
        "source": source,
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "function_name": "main",
        "inputs": {},
        "limits": {"cpu_seconds": 5, "memory_bytes": 134217728, "max_processes": 16, "max_output_bytes": 65536},
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(BOOTSTRAP),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    process.stdin.write(frame(payload))
    await process.stdin.drain()
    assert await asyncio.wait_for(receive(process.stdout), 3) == {"type": "result", "output": 7}
    assert await process.wait() == 0


@pytest.mark.asyncio
async def test_socket_transport_redirects_guest_output_before_loading_source():
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    source = (
        "def main(inputs):\n import os\n"
        ' os.write(1, b"stdout-marker"); os.write(2, b"stderr-marker")\n'
        ' os.write(1, b"Q" * 262144); os.write(2, b"R" * 262144)\n'
        ' return {"ok": True}\n'
    )
    payload = {
        "type": "start",
        "source": source,
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "function_name": "main",
        "inputs": {},
        "limits": {"cpu_seconds": 5, "memory_bytes": 134217728, "max_processes": 16, "max_output_bytes": 65536},
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(BOOTSTRAP),
        "--socket",
        str(port),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        for _ in range(100):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                break
            except OSError:
                await asyncio.sleep(0.02)
        else:
            pytest.fail("runner did not listen")
        writer.write(frame({"type": "probe"}))
        await writer.drain()
        assert await receive(reader) == {"type": "ready"}
        writer.write(frame(payload))
        await writer.drain()
        assert await receive(reader) == {"type": "result", "output": {"ok": True}}
        writer.close()
        await writer.wait_closed()
        assert await process.wait() == 0
        assert await process.stdout.read() == b""
        assert await process.stderr.read() == b""
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
