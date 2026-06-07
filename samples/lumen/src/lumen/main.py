# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""ASGI entry point — bootstraps PyFly and exposes the web ``app``.

``pyfly run`` (and any ASGI server such as ``uvicorn lumen.main:app``)
imports the module-level :data:`app` from here. :class:`PyFlyApplication`
loads ``pyfly.yaml``, scans the packages declared on
:class:`~lumen.app.LumenApplication`, and builds the DI context; the
Starlette adapter then mounts every ``@rest_controller`` it finds.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.staticfiles import StaticFiles

from pyfly.core import PyFlyApplication
from pyfly.web.adapters.starlette import create_app

from lumen.app import LumenApplication

# Bootstrap: load config, scan packages, build the DI context.
_pyfly = PyFlyApplication(LumenApplication)
_static_dir = Path(__file__).parent / "static"
_static_dir.mkdir(exist_ok=True)


@asynccontextmanager
async def _lifespan(app: Starlette) -> AsyncIterator[None]:
    """Manage application startup and shutdown lifecycle."""
    _pyfly._route_metadata = getattr(app.state, "pyfly_route_metadata", [])
    _pyfly._docs_enabled = getattr(app.state, "pyfly_docs_enabled", False)
    _pyfly._host = str(_pyfly.config.get("pyfly.web.host", "0.0.0.0"))
    _pyfly._port = int(_pyfly.config.get("pyfly.web.port", 8080))
    await _pyfly.startup()
    yield
    await _pyfly.shutdown()


app = create_app(
    title="lumen",
    version="1.0.0",
    description="Lumen — a DDD digital-wallet service built on the PyFly framework.",
    context=_pyfly.context,
    lifespan=_lifespan,
)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
