from pathlib import Path

import pytest
from starlette.testclient import TestClient

from pyfly.container import controller, rest_controller
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web import ModelAndView, Redirect, get_mapping


@controller
class Pages:
    @get_mapping("/page", status_code=202)
    async def page(self) -> ModelAndView:
        return ModelAndView("page.html", {"title": "<b>Ada</b>"})

    @get_mapping("/redirect")
    async def redirect(self) -> Redirect:
        return Redirect("/page")

    @get_mapping("/broken")
    async def broken(self) -> ModelAndView:
        raise RuntimeError("secret-password")


@rest_controller
class Api:
    @get_mapping("/api/broken")
    async def broken(self) -> dict:
        raise RuntimeError("secret-password")


@pytest.fixture(params=["starlette", "fastapi"])
async def webapp(request, tmp_path: Path):
    from importlib import import_module

    (tmp_path / "page.html").write_text("<h1>{{ title }}</h1>")
    (tmp_path / "errors").mkdir()
    (tmp_path / "errors" / "404.html").write_text("CUSTOM {{ status }} {{ path }}")
    (tmp_path / "style.css").write_text("body { color: red; }")
    ctx = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "web": {
                        "templates": {"enabled": True, "directories": [str(tmp_path)]},
                        "static": {"enabled": True, "directories": [str(tmp_path)]},
                        "errors": {"html-enabled": True},
                    }
                }
            }
        )
    )
    ctx.register_bean(Pages)
    ctx.register_bean(Api)
    await ctx.start()
    app = import_module(f"pyfly.web.adapters.{request.param}.app").create_app(context=ctx, docs_enabled=False)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client
    finally:
        await ctx.stop()


def test_page_and_static(webapp):
    response = webapp.get("/page")
    assert response.status_code == 202
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == "<h1>&lt;b&gt;Ada&lt;/b&gt;</h1>"
    assert webapp.get("/redirect", follow_redirects=False).status_code == 303
    assert webapp.get("/static/style.css").status_code == 200


def test_html_errors_and_api_compatibility(webapp):
    response = webapp.get("/missing", headers={"accept": "text/html"})
    assert response.status_code == 404
    assert "CUSTOM 404 /missing" in response.text
    response = webapp.get("/broken", headers={"accept": "text/html"})
    assert response.status_code == 500
    assert "secret-password" not in response.text
    assert response.headers["content-type"].startswith("text/html")
    response = webapp.get("/api/broken", headers={"accept": "text/html"})
    assert response.headers["content-type"].startswith("application/json")
    response = webapp.get("/missing", headers={"accept": "text/html;q=0,application/json"})
    assert not response.headers["content-type"].startswith("text/html")
