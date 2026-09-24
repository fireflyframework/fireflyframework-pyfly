"""Native welcome pages and diagnostic errors across both adapters."""

from contextlib import asynccontextmanager
from importlib import import_module

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from pyfly.container import bean, configuration, controller, rest_controller
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web import ModelAndView, get_mapping


@controller
class FailurePages:
    @get_mapping("/broken")
    async def broken(self) -> ModelAndView:
        raise RuntimeError("diagnostic <script>alert(1)</script>")


@rest_controller
class FailureApi:
    @get_mapping("/api/broken")
    async def broken(self) -> dict:
        raise RuntimeError("private-api-detail")


@controller
class HomePage:
    @get_mapping("/", name="home")
    async def home(self) -> ModelAndView:
        return ModelAndView("home.html")


@configuration
class LateHomeConfiguration:
    @bean
    def home_page(self) -> HomePage:
        return HomePage()


@pytest.fixture(params=["starlette", "fastapi"])
def make_app(request, tmp_path):
    def factory(*, web=None, debug=False, controllers=()):
        settings = {
            "templates": {"enabled": True, "directories": [str(tmp_path)]},
            "errors": {"html-enabled": True},
            **(web or {}),
        }
        context = ApplicationContext(Config({"pyfly": {"web": settings}}))
        for cls in (FailurePages, FailureApi, *controllers):
            context.register_bean(cls)

        @asynccontextmanager
        async def lifespan(app):
            await context.start()
            try:
                yield
            finally:
                await context.stop()

        return import_module(f"pyfly.web.adapters.{request.param}.app").create_app(
            context=context, docs_enabled=False, debug=debug, lifespan=lifespan
        )

    return factory


def test_bundled_welcome_and_assets_under_mount(make_app):
    app = make_app()
    parent = Starlette(routes=[Mount("/catalog", app=app, name="catalog")])
    # Mounted subapplications do not receive their parent's lifespan events.
    with TestClient(app), TestClient(parent) as client:
        response = client.get("/catalog/")
        assert response.status_code == 200
        assert "Welcome to PyFly" in response.text
        for filename in ("web.css", "theme.css", "logo.png"):
            path = f"/catalog/_pyfly/web/{filename}"
            assert path in response.text
            assert client.get(path).status_code == 200
        assert client.get("/catalog/_pyfly/web/../config.py").status_code == 404
        assert client.get("/catalog/_pyfly/web/missing.css").status_code == 404


def test_welcome_respects_application_home_and_restart(make_app, tmp_path):
    (tmp_path / "home.html").write_text("Application home")
    app = make_app(controllers=(HomePage,))
    for _ in range(2):
        with TestClient(app) as client:
            assert client.get("/").text == "Application home"
            assert len([r for r in app.routes if getattr(r, "name", "") == "pyfly_web_asset"]) == 1


def test_welcome_opt_out_and_custom_template(make_app, tmp_path):
    with TestClient(make_app(web={"welcome": {"enabled": False}})) as client:
        assert client.get("/").status_code == 404
    (tmp_path / "landing.html").write_text("Custom {{ branding.name }}")
    with TestClient(make_app(web={"welcome": {"template": "landing.html"}})) as client:
        assert client.get("/").text == "Custom PyFly"


def test_late_bean_home_controller_replaces_eager_welcome(make_app, tmp_path):
    (tmp_path / "home.html").write_text("Late home")
    app = make_app(controllers=(LateHomeConfiguration,))
    for _ in range(2):
        with TestClient(app) as client:
            assert client.get("/").text == "Late home"


@pytest.mark.parametrize(
    "debug,policy,visible",
    [(False, "on-debug", False), (True, "on-debug", True), (True, "never", False), (False, "always", True)],
)
def test_trace_policy_keeps_rest_json(make_app, debug, policy, visible):
    app = make_app(debug=debug, web={"errors": {"html-enabled": True, "include-stacktrace": policy}})
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/broken", headers={"accept": "text/html"})
        assert response.status_code == 500
        assert "PyFly" in response.text
        assert "<script>alert(1)</script>" not in response.text
        assert ("RuntimeError" in response.text) is visible
        assert ("test_branded_pages.py" in response.text) is visible
        assert response.headers["cache-control"] == "no-store"
        api = client.get("/api/broken", headers={"accept": "text/html"})
        assert api.status_code == 500
        assert "application/json" in api.headers["content-type"]
        assert "private-api-detail" not in api.text


def test_custom_errors_receive_trace_context_and_broken_template_falls_back(make_app, tmp_path):
    errors = tmp_path / "errors"
    errors.mkdir()
    error = errors / "500.html"
    error.write_text("CUSTOM {{ exception_type }} {{ stack_frames[-1].function }} {{ stack_trace }}")
    with TestClient(make_app(web={"debug": True}), raise_server_exceptions=False) as client:
        response = client.get("/broken")
        assert "CUSTOM RuntimeError broken" in response.text
        assert "&lt;script&gt;" in response.text
    error.write_text("{{ unknown.value }}")
    with TestClient(make_app(debug=True), raise_server_exceptions=False) as client:
        response = client.get("/broken")
        assert "PyFly" in response.text and "RuntimeError" in response.text
        assert "UndefinedError" not in response.text


def test_branding_is_shared_escaped_and_theme_is_csp_compatible(make_app):
    brand = {
        "name": "Acme <Portal>",
        "logo-url": "/images/acme.svg",
        "primary-color": "#123456",
        "accent-color": "#abcdef",
        "support-url": "https://example.org/help",
        "footer": "Acme team",
    }
    with TestClient(make_app(web={"branding": brand})) as client:
        for path in ("/", "/missing"):
            response = client.get(path, headers={"accept": "text/html"})
            assert "Acme &lt;Portal&gt;" in response.text
            assert 'src="/images/acme.svg"' in response.text
            assert "https://example.org/help" in response.text
            assert "Acme team" in response.text
            assert "<style" not in response.text and "style=" not in response.text
        css = client.get("/_pyfly/web/theme.css")
        assert "#123456" in css.text and "#abcdef" in css.text


def test_templates_disabled_does_not_install_welcome(make_app):
    with TestClient(make_app(web={"templates": {"enabled": False}})) as client:
        response = client.get("/", headers={"accept": "text/html"})
        assert response.status_code == 404 and "PyFly" in response.text


def test_api_only_installs_no_browser_routes_and_negotiates_no_html(make_app):
    app = make_app(web={"templates": {"enabled": False}, "errors": {"html-enabled": False}})
    with TestClient(app, raise_server_exceptions=False) as client:
        for path in ("/", "/_pyfly/web/web.css", "/api/broken"):
            response = client.get(path, headers={"accept": "text/html"})
            assert "text/html" not in response.headers["content-type"]
            if path == "/api/broken":
                assert "application/json" in response.headers["content-type"]
        assert not any(getattr(r, "name", "").startswith("pyfly_w") for r in app.routes)


def test_false_debug_environment_keeps_trace_hidden(make_app, monkeypatch):
    monkeypatch.setenv("PYFLY_WEB_DEBUG", "false")
    with TestClient(make_app(), raise_server_exceptions=False) as client:
        assert "RuntimeError" not in client.get("/broken").text


def test_invalid_branding_and_trace_configuration():
    from pyfly.web.templating.config import BrandingProperties, HtmlErrorProperties

    for values in (
        {"primary_color": "red;display:none"},
        {"logo_url": "javascript:alert(1)"},
        {"support_url": "//evil.test"},
        {"assets_path": "/"},
    ):
        with pytest.raises(ValueError):
            BrandingProperties(**values)
    with pytest.raises(ValueError):
        HtmlErrorProperties(include_stacktrace="sometimes")
