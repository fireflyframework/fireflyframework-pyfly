"""Named local URLs for Python code and server-rendered templates."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import quote


class _RoutingRequest(Protocol):
    @property
    def scope(self) -> Mapping[str, Any]: ...


def reverse(request: _RoutingRequest, name: str, /, **path_params: Any) -> str:
    """Resolve a named route to an encoded local path, including mount prefixes.

    Pass decoded parameter values. Unknown names or mismatched parameters raise
    the router's NoMatchFound error. This helper requires an active request.
    """
    router = request.scope.get("app") or request.scope.get("router")
    if router is None:
        raise RuntimeError("URL reversal requires an application request")
    # The outer router can contain sibling apps with identical route names.
    # Resolve locally, then include the complete active ASGI mount prefix.
    path = request.scope.get("root_path", "").rstrip("/") + str(router.url_path_for(name, **path_params))
    return quote(path, safe="/")


def static_url(request: _RoutingRequest, path: str) -> str:
    """Resolve a decoded path relative to the configured static resource root."""
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        or any(part in {".", ".."} for part in path.split("/"))
    ):
        raise ValueError("Static resource paths must be relative and cannot traverse directories")
    return reverse(request, "static", path=path)
