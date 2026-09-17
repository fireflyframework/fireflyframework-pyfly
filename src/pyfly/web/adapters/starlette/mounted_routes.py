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
"""What the OpenAPI document can say about routes that are not controllers.

``create_app(extra_routes=...)`` accepts plain Starlette ``Route`` objects and ``Mount``\\ s of
whole sub-applications — the shape a service uses for provider webhooks, an SDK's ASGI app or a
legacy surface it carries beside its ``@rest_controller`` classes. Until 26.09.05 those routes
were served and never described: the generator worked from controller metadata only, so a
document could lose the paths that carried the traffic without any diff noticing. This module
walks them. It cannot recover a typed contract (there is no handler signature to read), so what
it yields is the path, the method, a name, the endpoint's docstring summary and the path
parameters Starlette's convertors declare — enough for the document to be complete AND valid,
and marked so a reader knows the operation is described from the outside.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from starlette.routing import Mount, Route

#: Starlette adds HEAD to every GET route and answers OPTIONS itself; both are transport, not
#: contract, and listing them would double every path in the document.
_IMPLICIT_METHODS = frozenset({"HEAD", "OPTIONS"})

#: ``/{name}`` or ``/{name:convertor}`` — Starlette's path template syntax. OpenAPI knows only
#: ``{name}``; the convertor becomes the parameter's schema.
_TEMPLATE = re.compile(r"{([A-Za-z_][A-Za-z0-9_]*)(?::[A-Za-z_][A-Za-z0-9_]*)?}")

#: Starlette convertor name → (OpenAPI type, format). Unknown convertors (a registered custom
#: one) are strings: the only claim the document can make without reading the convertor.
_CONVERTOR_SCHEMAS: dict[str, tuple[str, str | None]] = {
    "str": ("string", None),
    "path": ("string", None),
    "int": ("integer", None),
    "float": ("number", None),
    "uuid": ("string", "uuid"),
}


@dataclass(frozen=True)
class MountedParameter:
    """A path parameter of a mounted route, as OpenAPI must declare it.

    OpenAPI 3.1 requires every ``{expression}`` in a path template to have a ``parameters``
    entry; a document that omits one does not validate, and the gateway's
    ``/api/telegram/{botId}/updates`` was emitted without it until 26.09.05. Controllers get
    theirs from the handler signature; a plain route has only Starlette's convertor to read.
    """

    name: str
    type: str
    format: str | None

    def to_openapi(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": self.type}
        if self.format:
            schema["format"] = self.format
        return {"name": self.name, "in": "path", "required": True, "schema": schema}


@dataclass(frozen=True)
class MountedRoute:
    """One operation discovered by walking the extra routes.

    ``method`` is ``None`` for a mount whose app could not be walked (a static-files app, a
    foreign ASGI callable): the prefix is known, what it serves is not. ``path`` is the OpenAPI
    template (``/{botId}/updates``), never Starlette's (``/{botId:int}/updates``); the
    convertors live in ``parameters``, mount prefix first.
    """

    path: str
    method: str | None
    name: str
    summary: str
    parameters: tuple[MountedParameter, ...] = ()


def _template_and_parameters(
    starlette_path: str, convertors: dict[str, Any]
) -> tuple[str, tuple[MountedParameter, ...]]:
    """Split a Starlette path into the OpenAPI template and its declared parameters."""
    parameters: list[MountedParameter] = []

    # Starlette keeps a convertor INSTANCE per parameter, not the ``{name:key}`` key it was
    # written with; the built-ins are recognised by their class, and a custom convertor an
    # application registered is described as a string — the only claim that cannot be wrong.
    from starlette.convertors import CONVERTOR_TYPES

    schema_by_class: dict[type, tuple[str, str | None]] = {
        type(convertor): _CONVERTOR_SCHEMAS[key]
        for key, convertor in CONVERTOR_TYPES.items()
        if key in _CONVERTOR_SCHEMAS
    }

    def _declare(match: re.Match[str]) -> str:
        name = match.group(1)
        convertor_class: type = type(convertors.get(name))
        openapi_type, openapi_format = schema_by_class.get(convertor_class, ("string", None))
        parameters.append(MountedParameter(name, openapi_type, openapi_format))
        return "{" + name + "}"

    template = _TEMPLATE.sub(_declare, starlette_path)
    return template, tuple(parameters)


def _summary(endpoint: Any) -> str:
    doc = inspect.getdoc(endpoint) or ""
    return doc.split("\n\n", 1)[0].strip().replace("\n", " ")


def _name(route: Any, endpoint: Any) -> str:
    explicit = getattr(route, "name", None)
    if explicit:
        return str(explicit)
    return str(getattr(endpoint, "__name__", None) or type(endpoint).__name__)


def collect_mounted_routes(routes: Iterable[Any], prefix: str = "") -> list[MountedRoute]:
    """Walk *routes* (``Route`` and ``Mount`` objects, recursively) into :class:`MountedRoute` rows.

    A ``Mount`` whose app exposes ``routes`` (a Starlette app or router) is entered with its path
    as the prefix; any other mount is reported once with ``method=None``.
    """
    return _collect(routes, prefix, ())


def _collect(routes: Iterable[Any], prefix: str, inherited: tuple[MountedParameter, ...]) -> list[MountedRoute]:
    found: list[MountedRoute] = []
    for route in routes:
        if isinstance(route, Route):
            template, own = _template_and_parameters(route.path, getattr(route, "param_convertors", {}) or {})
            path = f"{prefix}{template}"
            methods = sorted((route.methods or set()) - _IMPLICIT_METHODS)
            for method in methods:
                found.append(
                    MountedRoute(path, method, _name(route, route.endpoint), _summary(route.endpoint), inherited + own)
                )
            continue
        if isinstance(route, Mount):
            template, own = _template_and_parameters(route.path, getattr(route, "param_convertors", {}) or {})
            mount_path = f"{prefix}{template}"
            inner = getattr(route.app, "routes", None)
            if inner is None:
                found.append(MountedRoute(mount_path, None, _name(route, route.app), "", inherited + own))
                continue
            found.extend(_collect(inner, mount_path, inherited + own))
    return found
