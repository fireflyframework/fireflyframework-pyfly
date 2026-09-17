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
it yields is the path, the method, a name and the endpoint's docstring summary — enough for the
document to be complete, and marked so a reader knows the operation is described from the
outside.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from starlette.routing import Mount, Route

#: Starlette adds HEAD to every GET route and answers OPTIONS itself; both are transport, not
#: contract, and listing them would double every path in the document.
_IMPLICIT_METHODS = frozenset({"HEAD", "OPTIONS"})


@dataclass(frozen=True)
class MountedRoute:
    """One operation discovered by walking the extra routes.

    ``method`` is ``None`` for a mount whose app could not be walked (a static-files app, a
    foreign ASGI callable): the prefix is known, what it serves is not.
    """

    path: str
    method: str | None
    name: str
    summary: str


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
    found: list[MountedRoute] = []
    for route in routes:
        if isinstance(route, Route):
            path = f"{prefix}{route.path}"
            methods = sorted((route.methods or set()) - _IMPLICIT_METHODS)
            for method in methods:
                found.append(MountedRoute(path, method, _name(route, route.endpoint), _summary(route.endpoint)))
            continue
        if isinstance(route, Mount):
            mount_path = f"{prefix}{route.path}"
            inner = getattr(route.app, "routes", None)
            if inner is None:
                found.append(MountedRoute(mount_path, None, _name(route, route.app), ""))
                continue
            found.extend(collect_mounted_routes(inner, prefix=mount_path))
    return found
