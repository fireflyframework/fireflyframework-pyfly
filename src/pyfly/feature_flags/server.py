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
"""Serve the effective flagd document to authenticated sync clients."""

from __future__ import annotations

import hashlib
import hmac
import json
import posixpath
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext
    from pyfly.feature_flags.registry import FlagRegistry

__all__ = ["DEFAULT_PATH", "FlagSyncServer", "build_feature_flag_server_routes"]

DEFAULT_PATH = "/feature-flags/flagd.json"


def _root_path(path: str) -> bool:
    """Refuse paths which Starlette can normalize to the application's root."""
    return posixpath.normpath("/" + unquote(path).lstrip("/")) == "/"


def _etags(header: str | None) -> set[str]:
    if not header:
        return set()
    return {part.strip().removeprefix("W/") for part in header.split(",")}


class FlagSyncServer:
    """Read-only HTTP route over the registry's composed document."""

    def __init__(
        self, registry: FlagRegistry, *, path: str = DEFAULT_PATH, token: str = "", allow_anonymous: bool = False
    ) -> None:
        if not token and not allow_anonymous:
            raise ValueError("the feature flag sync server needs a token, or allow-anonymous=true")
        if _root_path(path):
            raise ValueError("the feature flag sync server path must not be the root")
        if not path.startswith("/"):
            raise ValueError("the feature flag sync server path must start with /")
        self._registry = registry
        self._path = path
        self._expected = f"Bearer {token}".encode() if token else None

    @property
    def path(self) -> str:
        return self._path

    def body_and_etag(self) -> tuple[bytes, str]:
        document = self._registry.document(include_test_overrides=False)
        body = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return body, f'"{hashlib.sha256(body).hexdigest()}"'

    def authorized(self, authorization: str | None) -> bool:
        if self._expected is None:
            return True
        return authorization is not None and hmac.compare_digest(authorization.encode(), self._expected)

    async def handle(self, request: Any) -> Any:
        from starlette.responses import JSONResponse, Response

        if not self.authorized(request.headers.get("authorization")):
            return JSONResponse(
                {"error": "unauthorized", "message": "a valid bearer token is required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        body, etag = self.body_and_etag()
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        conditions = _etags(request.headers.get("if-none-match"))
        if etag in conditions or "*" in conditions:
            return Response(status_code=304, headers=headers)
        return Response(body, media_type="application/json", headers=headers)

    def routes(self) -> list[Any]:
        from starlette.routing import Route

        return [Route(self._path, self.handle, methods=["GET"], name="pyfly.feature_flags.sync")]


def build_feature_flag_server_routes(context: ApplicationContext | None) -> list[Any]:
    """Return the registered sync server's route, if the context has one."""
    if context is None:
        return []
    for registration in list(context.container._registrations.values()):
        if isinstance(registration.instance, FlagSyncServer):
            return registration.instance.routes()
    return []
