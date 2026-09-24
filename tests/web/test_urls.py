from importlib import import_module
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Mount, NoMatchFound, Route
from starlette.testclient import TestClient

from pyfly.container import controller
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web import ModelAndView, PathVar, Redirect, get_mapping


@controller
class LinkedPages:
    @get_mapping("/", name="home")
    async def home(self, request: Request) -> ModelAndView:
        from pyfly.web import reverse, static_url

        return ModelAndView(
            "links.html",
            {"asset": static_url(request, "files/café #1?.txt"), "detail": reverse(request, "detail", slug="café #1?")},
        )

    @get_mapping("/details/{slug}", name="detail")
    async def detail(self, slug: PathVar[str]) -> str:
        return slug

    @get_mapping("/go")
    async def go(self, request: Request) -> Redirect:
        from pyfly.web import reverse

        return Redirect(reverse(request, "home"))

    @get_mapping("/fail")
    async def fail(self) -> ModelAndView:
        raise RuntimeError("private-error-details")


@pytest.mark.parametrize("adapter", ["starlette", "fastapi"])
@pytest.mark.parametrize("mount_name", [None, "shop"])
async def test_python_and_template_links_resolve_assets_and_routes_under_mount(adapter, mount_name, tmp_path: Path):
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "café #1?.txt").write_text("downloaded")
    (tmp_path / "links.html").write_text(
        "{{ static_url('files/café #1?.txt') }}|{{ asset }}|{{ reverse('detail', slug='café #1?') }}|{{ detail }}"
    )
    (tmp_path / "errors").mkdir()
    for status in (404, 500):
        (tmp_path / "errors" / f"{status}.html").write_text(
            "ERROR {{ status }}|{{ static_url('files/café #1?.txt') }}|{{ reverse('home') }}|{{ message }}"
        )
    context = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "web": {
                        "templates": {"enabled": True, "directories": [str(tmp_path)]},
                        "static": {"enabled": True, "path": "/assets", "directories": [str(tmp_path)]},
                        "errors": {"html-enabled": True},
                    }
                }
            }
        )
    )
    context.register_bean(LinkedPages)
    await context.start()
    app = import_module(f"pyfly.web.adapters.{adapter}.app").create_app(context=context, docs_enabled=False)
    try:
        parent = Starlette(
            routes=[Route("/parent", lambda request: None, name="home"), Mount("/shop", app=app, name=mount_name)]
        )
        with TestClient(parent, root_path="/proxy", raise_server_exceptions=False) as client:
            response = client.get("/proxy/shop/")
            assert response.status_code == 200, response.text
            asset, code_asset, detail, code_detail = response.text.split("|")
            assert asset == code_asset == "/proxy/shop/assets/files/caf%C3%A9%20%231%3F.txt"
            assert detail == code_detail == "/proxy/shop/details/caf%C3%A9%20%231%3F"
            assert client.get(asset).text == "downloaded"
            assert client.get(detail).json() == "café #1?"
            assert client.get("/proxy/shop/go", follow_redirects=False).headers["location"] == "/proxy/shop/"
            for path, status in (("missing", 404), ("fail", 500)):
                error = client.get(f"/proxy/shop/{path}", headers={"Accept": "text/html"})
                assert error.status_code == status
                assert f"ERROR {status}|{asset}|/proxy/shop/|" in error.text
                assert "private-error-details" not in error.text
    finally:
        await context.stop()


@pytest.mark.parametrize(
    "path", ["", "/private.txt", "//host/file", "../secret", "a/../secret", "a\\secret", "a\nfile"]
)
def test_static_url_rejects_paths_outside_asset_namespace(path):
    from pyfly.web import static_url

    with pytest.raises(ValueError):
        static_url(Request({"type": "http"}), path)


def test_missing_named_route_fails_instead_of_inventing_a_url():
    from pyfly.web import reverse

    app = Starlette(routes=[Route("/", lambda request: None, name="home")])
    request = Request(
        {
            "type": "http",
            "app": app,
            "scheme": "http",
            "server": ("test", 80),
            "path": "/",
            "root_path": "",
            "headers": [],
        }
    )
    with pytest.raises(NoMatchFound):
        reverse(request, "missing")


def test_route_parameter_named_name_is_not_confused_with_route_name():
    from pyfly.web import reverse

    app = Starlette(routes=[Route("/users/{name}", lambda request: None, name="profile")])
    request = Request({"type": "http", "app": app, "root_path": "/portal"})
    assert reverse(request, "profile", name="Ada Lovelace") == "/portal/users/Ada%20Lovelace"
