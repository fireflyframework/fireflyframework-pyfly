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
"""``ManagementRoutesContributor`` — an application's own routes on the management surface.

The actuator's read model (``GET /actuator/{id}`` with query parameters, plus a
``@write_operation`` POST with a JSON document) covers a management *endpoint*. It does not
cover a management *operation* that needs its own request shape, its own authentication or
a response that is not a JSON object — the kind of thing a control plane asks a running
service to do on its internal port. Before this port existed the only way to mount such a
route on the management listener was to replace ``create_management_app`` on its module at
boot; the framework now asks the container instead.

A bean implementing this protocol has its routes mounted:

* on the management app when ``pyfly.management.server.port`` selects a separate listener,
  under the configured base path;
* on the main app when the management surface is shared (the default), so an application
  that runs shared in tests and separate in production sees one behaviour — whether the
  context was started before ``create_app`` or, as the generated ``main.py`` does, inside the
  app's lifespan (the post-start rescan mounts what the build-time scan could not see);
* nowhere when management is disabled (``port: -1``), like the actuator itself.

The routes are Starlette ``BaseRoute`` objects; the type is ``Any`` here because the ports
package carries no vendor import — Starlette lives only in the adapters.

The management port carries none of the application's security filters unless
``pyfly.management.security.enabled`` is on, so a contributed route that must not be public
authenticates itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ManagementRoutesContributor(Protocol):
    """A bean whose :meth:`management_routes` are mounted on the management surface."""

    def management_routes(self) -> Sequence[Any]:
        """Return the Starlette routes to mount. Called once, while the app is built."""
        ...


def collect_management_routes(context: Any, *, asked: set[int] | None = None) -> list[Any]:
    """Every route every *instantiated* :class:`ManagementRoutesContributor` bean contributes.

    Beans are visited in registration order and each contributor is asked once, even when it
    is registered under several keys (its class and the protocols it satisfies).

    Only instances are visited, never factories: a contributor is an ordinary bean and building
    it here would run its constructor outside the container's start sequence. That has a
    consequence the caller must handle. The canonical boot order (``main.py.j2``) is
    ``create_app(context=ctx, lifespan=...)`` at import time with ``ctx.start()`` INSIDE the
    lifespan, so at build time no user bean exists yet and this function finds nothing — the
    same reason the actuator's health indicators and ``@bean``-produced controllers are
    rescanned after start. ``create_app`` therefore calls this twice: once while building (a
    context started beforehand — tests, embedded use — is served immediately) and once from the
    post-start rescan. *asked* carries the ids of the contributors already visited across those
    calls so ``management_routes()`` runs once per bean and no route is mounted twice.
    """
    routes: list[Any] = []
    seen: set[int] = asked if asked is not None else set()
    for reg in context.container._registrations.values():
        instance = reg.instance
        if instance is None or id(instance) in seen or not isinstance(instance, ManagementRoutesContributor):
            continue
        seen.add(id(instance))
        routes.extend(instance.management_routes())
    return routes
