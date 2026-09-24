import pytest

from pyfly.admin.data.config import AdminDataProperties
from pyfly.core.config import Config
from pyfly.web.forms import FormProperties
from pyfly.web.templating.config import HtmlErrorProperties, StaticProperties, TemplateProperties


@pytest.mark.parametrize(
    "extension,content",
    [
        (
            "yaml",
            "pyfly:\n  web:\n    templates:\n      enabled: true\n      directories: [pages]\n      cache-size: 9\n",
        ),
        ("toml", '[pyfly.web.templates]\nenabled = true\ndirectories = ["pages"]\ncache-size = 9\n'),
    ],
)
def test_file_and_environment_binding(tmp_path, monkeypatch, extension, content):
    path = tmp_path / f"config.{extension}"
    path.write_text(content)
    monkeypatch.setenv("PYFLY_WEB_TEMPLATES_CACHE_SIZE", "0")
    config = Config.from_file(path, load_defaults=False)
    props = config.bind(TemplateProperties)
    assert props.enabled and props.directories == ["pages"] and props.cache_size == 0
    monkeypatch.setenv("PYFLY_WEB_FORMS_MAX_BODY_SIZE", "1234")
    monkeypatch.setenv("PYFLY_ADMIN_DATA_ALLOWED_ROLES", "EDITOR")
    assert config.bind(FormProperties).max_body_size == 1234
    assert config.bind(AdminDataProperties).allowed_roles == ["EDITOR"]


def test_profiles_and_defaults(tmp_path):
    (tmp_path / "pyfly.yaml").write_text("pyfly:\n  admin:\n    data:\n      enabled: true\n      page-size: 20\n")
    (tmp_path / "pyfly-test.yaml").write_text("pyfly:\n  admin:\n    data:\n      page-size: 5\n")
    config = Config.from_sources(tmp_path, active_profiles=["test"], load_defaults=False)
    assert config.bind(AdminDataProperties).page_size == 5
    assert not Config().bind(TemplateProperties).enabled
    assert not Config().bind(StaticProperties).enabled
    assert not Config().bind(HtmlErrorProperties).html_enabled
    assert not Config().bind(AdminDataProperties).enabled


@pytest.mark.parametrize(
    "factory",
    [
        lambda: TemplateProperties(cache_size=-1),
        lambda: FormProperties(max_fields=0),
        lambda: AdminDataProperties(max_page_size=0),
        lambda: AdminDataProperties(enabled=True, allowed_roles=[]),
    ],
)
def test_invalid_limits_fail_early(factory):
    with pytest.raises(ValueError):
        factory()


async def test_user_engine_bean_takes_precedence_over_missing_jinja_root():
    from pyfly.context.application_context import ApplicationContext
    from pyfly.web.templating.ports import TemplateEngine

    class Engine:
        async def render(self, template, context):
            return context["title"]

    engine = Engine()
    context = ApplicationContext(
        Config({"pyfly": {"web": {"templates": {"enabled": True, "directories": ["/nonexistent-root"]}}}})
    )
    context.container.register_instance(TemplateEngine, engine)
    await context.start()
    try:
        assert context.get_bean(TemplateEngine) is engine
        assert await engine.render("ignored", {"title": "Custom"}) == "Custom"
    finally:
        await context.stop()


async def test_template_and_static_symlinks_cannot_escape_roots(tmp_path):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from pyfly.web.adapters.starlette.static_resources import build_static_routes
    from pyfly.web.templating.adapters.jinja import JinjaTemplateEngine
    from pyfly.web.templating.ports import TemplateNotFoundError

    root = tmp_path / "public"
    root.mkdir()
    outside = tmp_path / "private.html"
    outside.write_text("PRIVATE")
    (root / "link.html").symlink_to(outside)
    engine = JinjaTemplateEngine(TemplateProperties(directories=[str(root)]))
    with pytest.raises(TemplateNotFoundError):
        await engine.render("link.html", {})
    app = Starlette(routes=build_static_routes(StaticProperties(enabled=True, directories=[str(root)])))
    with TestClient(app) as client:
        assert client.get("/static/link.html").status_code == 404
        assert client.get("/static/%2e%2e/private.html").status_code == 404
