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

from typing import Any, Generic, TypeVar, cast

T = TypeVar("T")


class PathVar(Generic[T]):
    """Path variable extracted from the URL path (e.g. ``/orders/{order_id}``)."""


class QueryParam(Generic[T]):
    """Query parameter extracted from the URL query string (e.g. ``?page=1``)."""


class Body(Generic[T]):
    """JSON request body, validated via Pydantic when T is a BaseModel."""


class Header(Generic[T]):
    """HTTP header value. Parameter name is converted: ``x_api_key`` -> ``x-api-key``."""


class Cookie(Generic[T]):
    """Cookie value extracted from the request."""


class Valid(Generic[T]):
    """Marks a parameter for explicit Pydantic validation with structured 422 errors.

    Standalone usage (implies Body[T] + validation)::

        async def create(self, body: Valid[CreateOrderDTO]) -> OrderResponse: ...

    Wrapping a binding type (validate after resolution)::

        async def search(self, filters: Valid[QueryParam[SearchFilters]]) -> list: ...
        async def create(self, body: Valid[Body[CreateOrderDTO]]) -> OrderResponse: ...
    """


class File(Generic[T]):
    """Multipart file upload parameter.

    Single file::

        async def upload(self, file: File[UploadedFile]) -> dict: ...

    Multiple files::

        async def upload(self, files: File[list[UploadedFile]]) -> dict: ...
    """


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
