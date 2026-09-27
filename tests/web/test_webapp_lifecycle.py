from contextlib import asynccontextmanager
from importlib import import_module
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

from pyfly.container import component, controller
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web import ModelAndView, get_mapping


@controller
class RestartPages:
    def __init__(self):
        self.identity = uuid4().hex

    @get_mapping("/identity", name="identity")
    async def identity_page(self) -> ModelAndView:
        return ModelAndView("identity.html", {"identity": self.identity})


_LIFECYCLE: list[tuple[str, int]] = []


@component
class RestartPoller:
    """A scanned lifecycle bean: started and stopped once per lifespan, never the previous run's."""

    async def start(self) -> None:
        _LIFECYCLE.append(("start", id(self)))

    async def stop(self) -> None:
        _LIFECYCLE.append(("stop", id(self)))


@pytest.mark.parametrize("adapter", ["starlette", "fastapi"])
def test_restart_rebuilds_controller_and_engine(adapter, tmp_path):
    (tmp_path / "identity.html").write_text("{{ identity }}")
    context = ApplicationContext(
        Config({"pyfly": {"web": {"templates": {"enabled": True, "directories": [str(tmp_path)]}}}})
    )
    context.register_bean(RestartPages)
    context.register_bean(RestartPoller)
    _LIFECYCLE.clear()

    @asynccontextmanager
    async def lifespan(app):
        await context.start()
        try:
            yield
        finally:
            await context.stop()

    app = import_module(f"pyfly.web.adapters.{adapter}.app").create_app(
        context=context, lifespan=lifespan, docs_enabled=False
    )
    with TestClient(app) as client:
        first = client.get("/identity").text
        engine = app.state.pyfly_template_engine
    with TestClient(app) as client:
        assert client.get("/identity").text != first
        assert app.state.pyfly_template_engine is not engine
        assert len([route for route in app.routes if getattr(route, "name", "") == "identity"]) == 1
    # One start and one stop per lifespan, each on that lifespan's own instance.
    assert [event for event, _ in _LIFECYCLE] == ["start", "stop", "start", "stop"]
    assert _LIFECYCLE[0][1] == _LIFECYCLE[1][1] != _LIFECYCLE[2][1] == _LIFECYCLE[3][1]
