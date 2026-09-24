from pathlib import Path

import pytest


async def test_template_inheritance_escapes_input(tmp_path: Path) -> None:
    from pyfly.web.templating.adapters.jinja import JinjaTemplateEngine
    from pyfly.web.templating.config import TemplateProperties

    (tmp_path / "base.html").write_text("<main>{% block body %}{% endblock %}</main>")
    (tmp_path / "page.html").write_text('{% extends "base.html" %}{% block body %}{{ title }}{% endblock %}')
    engine = JinjaTemplateEngine(TemplateProperties(enabled=True, directories=[str(tmp_path)]))
    assert await engine.render("page.html", {"title": "<b>Ada</b>"}) == "<main>&lt;b&gt;Ada&lt;/b&gt;</main>"


async def test_missing_and_broken_templates_are_distinct(tmp_path: Path) -> None:
    from pyfly.web.templating.adapters.jinja import JinjaTemplateEngine
    from pyfly.web.templating.config import TemplateProperties
    from pyfly.web.templating.ports import TemplateNotFoundError

    engine = JinjaTemplateEngine(TemplateProperties(directories=[str(tmp_path)]))
    with pytest.raises(TemplateNotFoundError):
        await engine.render("../outside.html", {})
    (tmp_path / "broken.html").write_text("{{ missing }}")
    with pytest.raises(Exception, match="missing"):
        await engine.render("broken.html", {})


async def test_template_context_is_isolated(tmp_path: Path) -> None:
    import asyncio

    from pyfly.web.templating.adapters.jinja import JinjaTemplateEngine
    from pyfly.web.templating.config import TemplateProperties

    (tmp_path / "page.html").write_text("{{ name }}")
    engine = JinjaTemplateEngine(TemplateProperties(directories=[str(tmp_path)]))
    results = await asyncio.gather(*(engine.render("page.html", {"name": str(n)}) for n in range(20)))
    assert results == [str(n) for n in range(20)]
