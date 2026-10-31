from __future__ import annotations

import asyncio
import struct

import pytest

from pyfly.agentic.sandbox.framing import FrameError, read_frame, write_frame


@pytest.mark.asyncio
async def test_rejects_truncated_oversize_and_invalid_frames():
    for data in [b"\x00\x00", struct.pack(">I", 65), struct.pack(">I", 4) + b"no", struct.pack(">I", 1) + b"{"]:
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        with pytest.raises(FrameError):
            await read_frame(reader, limit=64)


@pytest.mark.asyncio
async def test_frame_round_trip_with_bounded_output():
    class Writer:
        def __init__(self):
            self.data = b""

        def write(self, value):
            self.data += value

        async def drain(self):
            pass

    writer = Writer()
    await write_frame(writer, {"x": 1}, limit=64)
    reader = asyncio.StreamReader()
    reader.feed_data(writer.data)
    reader.feed_eof()
    assert await read_frame(reader, limit=64) == {"x": 1}
