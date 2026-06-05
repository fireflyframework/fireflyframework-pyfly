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
"""Request binding types for controller handler methods.

Usage in handler signatures::

    async def get_order(self, order_id: PathVar[str]) -> OrderResponse: ...
    async def list_orders(self, page: QueryParam[int] = 1) -> list: ...
    async def create_order(self, body: Body[CreateOrderRequest]) -> OrderResponse: ...
    async def get_with_auth(self, token: Header[str]) -> dict: ...
    async def tracked(self, session: Cookie[str]) -> dict: ...
"""

from __future__ import annotations

from typing import Annotated, Any, TypeVar, cast, get_args

T = TypeVar("T")


class _Binding:
    """Internal sentinel identifying a request-binding source.

    Carried in the ``Annotated`` metadata of the public marker aliases below and
    recovered at startup by :func:`inspect_binding`. Private — handler authors
    use the public ``PathVar`` / ``Body`` / ... aliases, never this class.
    """

    __slots__ = ("kind",)

    def __init__(self, kind: str) -> None:
        self.kind = kind

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"_Binding({self.kind!r})"


_PATH = _Binding("path")
_QUERY = _Binding("query")
_BODY = _Binding("body")
_HEADER = _Binding("header")
_COOKIE = _Binding("cookie")
_FILE = _Binding("file")
_VALID = _Binding("validate")


# Public request-binding markers. Each is an ``Annotated`` alias, so a type
# checker sees ``PathVar[str]`` as ``str`` (and ``Valid[Body[Order]]`` as
# ``Order``) — handler bodies pass ``mypy --strict`` — while the binder recovers
# the sentinel from the annotation metadata at runtime via ``inspect_binding``.
PathVar = Annotated[T, _PATH]
"""Path variable extracted from the URL path (e.g. ``/orders/{order_id}``)."""

QueryParam = Annotated[T, _QUERY]
"""Query parameter extracted from the URL query string (e.g. ``?page=1``)."""

Body = Annotated[T, _BODY]
"""JSON request body, validated via Pydantic when the inner type is a BaseModel."""

Header = Annotated[T, _HEADER]
"""HTTP header value. Parameter name is converted: ``x_api_key`` -> ``x-api-key``."""

Cookie = Annotated[T, _COOKIE]
"""Cookie value extracted from the request."""

File = Annotated[T, _FILE]
"""Multipart file upload: ``File[UploadedFile]`` or ``File[list[UploadedFile]]``."""

Valid = Annotated[T, _VALID]
"""Validate the parameter with Pydantic, raising a structured 422 on failure.

Standalone (``Valid[CreateOrderDTO]``) implies a validated body. Wrapping a
binding (``Valid[Body[T]]`` / ``Valid[QueryParam[T]]``) validates after resolution.
"""


_BINDING_BY_SENTINEL: dict[_Binding, Any] = {
    _PATH: PathVar,
    _QUERY: QueryParam,
    _BODY: Body,
    _HEADER: Header,
    _COOKIE: Cookie,
    _FILE: File,
}


def inspect_binding(hint: Any) -> tuple[Any, Any, bool]:
    """Inspect a (possibly ``Annotated``) type hint for request-binding metadata.

    Returns ``(binding, inner_type, validate)`` where ``binding`` is the public
    marker alias (``PathVar``/``QueryParam``/``Body``/``Header``/``Cookie``/``File``)
    identifying the source — or ``None`` if the hint carries no binding marker —
    ``inner_type`` is the underlying type (``Annotated[X, ...]`` -> ``X``), and
    ``validate`` is ``True`` when the hint is wrapped in :data:`Valid`.

    Nested markers flatten (``Valid[Body[Order]]`` -> ``Annotated[Order, _BODY,
    _VALID]``). A standalone ``Valid[Model]`` (no binding marker) implies
    :data:`Body`, matching the legacy semantics.
    """
    metadata = getattr(hint, "__metadata__", ())
    if not metadata:
        return None, hint, False
    validate = _VALID in metadata
    binding: Any = None
    for marker in metadata:
        if isinstance(marker, _Binding) and marker is not _VALID:
            binding = _BINDING_BY_SENTINEL.get(marker)
            if binding is not None:
                break
    args = get_args(hint)
    inner_type = args[0] if args else str
    if binding is None and validate:
        binding = Body  # standalone Valid[Model] -> validated body
    return binding, inner_type, validate


class UploadedFile:
    """Represents an uploaded file from a multipart request.

    Attributes:
        filename: Original filename from the client.
        content_type: MIME type of the uploaded file.
        size: File size in bytes.
    """

    def __init__(
        self,
        filename: str,
        content_type: str,
        size: int,
        _file: Any,
    ) -> None:
        self._filename = filename
        self._content_type = content_type
        self._size = size
        self._file = _file

    @property
    def filename(self) -> str:
        return self._filename

    @property
    def content_type(self) -> str:
        return self._content_type

    @property
    def size(self) -> int:
        return self._size

    async def read(self) -> bytes:
        """Read the entire file content into memory."""
        if hasattr(self._file, "read"):
            data = self._file.read()
            if hasattr(data, "__await__"):
                return cast(bytes, await data)
            return cast(bytes, data)
        return b""

    async def save(self, path: Any) -> None:
        """Save the file to the given path."""
        from pathlib import Path

        content = await self.read()
        Path(path).write_bytes(content)
