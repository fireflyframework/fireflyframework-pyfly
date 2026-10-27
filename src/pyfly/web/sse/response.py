# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""SSE response utilities — event formatting and emitter (vendor-neutral).

Starlette-specific helpers (``make_sse_response``) live in
:mod:`pyfly.web.sse.adapters.starlette` to satisfy hexagonal architecture
constraints.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

T = TypeVar("T")


class _Missing:
    pass


_MISSING = _Missing()


@dataclass(frozen=True)
class SseEvent(Generic[T]):
    """Typed metadata; omitting data permits a comment-only heartbeat."""

    data: T | _Missing = _MISSING
    event: str | None = None
    id: str | None = None
    retry: int | None = None
    comment: str | None = None

    def encode(self, *, max_bytes: int | None = None) -> str:
        frame = format_sse_event(self.data, event=self.event, id=self.id, retry=self.retry, comment=self.comment)
        if max_bytes is not None and len(frame.encode("utf-8")) > max_bytes:
            raise ValueError("SSE event exceeds the configured size limit")
        return frame


def format_sse_event(
    data: Any,
    event: str | None = None,
    id: str | None = None,
    retry: int | None = None,
    *,
    comment: str | None = None,
) -> str:
    """Format a single Server-Sent Event string.

    Handles Pydantic ``BaseModel`` (via ``model_dump_json``), ``dict``/``list``
    (via ``json.dumps``), and raw strings.

    Parameters
    ----------
    data:
        The event payload.  Models and collections are JSON-serialized.
    event:
        Optional event type name (maps to the ``event:`` field).
    id:
        Optional event ID (maps to the ``id:`` field).
    retry:
        Optional reconnection time in milliseconds (maps to the ``retry:`` field).

    Returns
    -------
    str
        A fully-formed SSE event string terminated by a double newline.
    """
    lines: list[str] = []

    for name, value in (("id", id), ("event", event)):
        if value is not None and (not isinstance(value, str) or any(c in value for c in ("\r", "\n", "\0"))):
            raise ValueError(f"SSE {name} must be a single line without NUL")
    if retry is not None and (type(retry) is not int or retry < 0):
        raise ValueError("SSE retry must be a nonnegative integer")
    if comment is not None:
        if not isinstance(comment, str) or "\0" in comment:
            raise ValueError("SSE comment must be text without NUL")
        lines.extend(": " + line for line in comment.replace("\r\n", "\n").replace("\r", "\n").split("\n"))

    if id is not None:
        lines.append(f"id: {id}")
    if event is not None:
        lines.append(f"event: {event}")
    if retry is not None:
        lines.append(f"retry: {retry}")

    if isinstance(data, _Missing):
        return "\n".join([*lines, "", ""])
    if isinstance(data, BaseModel):
        payload = data.model_dump_json()
    elif data is None or isinstance(data, (dict, list, bool, int, float)):
        payload = json.dumps(data, allow_nan=False)
    else:
        payload = str(data)

    for line in payload.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        lines.append(f"data: {line}")

    lines.append("")
    lines.append("")
    return "\n".join(lines)


class SseEmitter:
    """High-level SSE emitter that wraps queued events.

    Provides a ``send`` interface for building SSE events.  The emitter
    is itself an async iterable so it can be passed to an adapter's
    streaming response factory.

    Usage::

        emitter = SseEmitter()
        emitter.send({"price": 42.0}, event="tick")
        emitter.send({"price": 43.5}, event="tick")

        # Pass to adapter layer for response creation
        async for chunk in emitter:
            ...
    """

    def __init__(self) -> None:
        self._queue: list[str] = []
        self._closed = False

    def send(self, data: Any, event: str | None = None, id: str | None = None) -> None:
        """Enqueue an SSE event for delivery.

        Parameters
        ----------
        data:
            The event payload.
        event:
            Optional event type name.
        id:
            Optional event ID.
        """
        if self._closed:
            return
        self._queue.append(format_sse_event(data, event=event, id=id))

    def close(self) -> None:
        """Mark the emitter as closed — no further events will be accepted."""
        self._closed = True

    async def __aiter__(self) -> AsyncGenerator[str, None]:
        """Iterate over queued events, yielding each as an SSE string."""
        for item in self._queue:
            yield item
