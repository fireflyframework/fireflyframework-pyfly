"""Exercise built wheels outside the checkout, with independent optional extras."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("extra", ["", "web", "templates", "webapp", "webapp,data-relational", "webapp,data-document"])
def test_installed_wheel_optional_extras(tmp_path, extra):
    wheel = os.environ.get("PYFLY_TEST_WHEEL")
    if not wheel:
        pytest.skip("Set PYFLY_TEST_WHEEL to the built wheel to run distribution validation")
    wheel = Path(wheel).resolve()
    assert wheel.is_file()
    environment = tmp_path / "venv"
    subprocess.run(["uv", "venv", str(environment), "--python", sys.executable], check=True, capture_output=True)
    python = environment / "bin/python"
    install = subprocess.run(
        ["uv", "pip", "install", "--python", str(python), str(wheel) + (f"[{extra}]" if extra else "")],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert install.returncode == 0, install.stderr
    script = """
import asyncio
from importlib.resources import files
from pathlib import Path
from pyfly.web import ModelAndView, Redirect, Form, reverse, static_url
from pyfly.admin import ModelAdmin
assert files('pyfly.admin').joinpath('static/js/views/datasources.js').is_file()
assert files('pyfly.cli').joinpath('templates/web/home_controller.py.j2').is_file()
assert files('pyfly.web.templating').joinpath('static/logo.png').is_file()
assert files('pyfly.web.templating').joinpath('templates/pyfly/welcome.html').is_file()
"""
    if "templates" in extra or "webapp" in extra:
        script += """
from pyfly.web.templating.adapters.jinja import JinjaTemplateEngine
from pyfly.web.templating.config import TemplateProperties
Path('templates').mkdir()
Path('templates/page.html').write_text('<h1>{{ title }}</h1>')
engine = JinjaTemplateEngine(TemplateProperties(directories=['templates']))
assert asyncio.run(engine.render('page.html', {'title': '<unsafe>'})) == '<h1>&lt;unsafe&gt;</h1>'
branding = dict(name='PyFly', tagline='Built to fly', logo_url='/logo.png', favicon_url='',
                documentation_url='', support_url='', footer='PyFly')
welcome = asyncio.run(engine.render('pyfly/welcome.html', {
    'branding': branding, 'web_asset_url': lambda name: '/_pyfly/web/' + name, 'home_url': '/'}))
assert 'Welcome to PyFly' in welcome
"""
    if extra == "web":
        script += """
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web.adapters.starlette.app import create_app
context = ApplicationContext(Config({'pyfly': {'web': {'errors': {'html-enabled': True}}}}))
asyncio.run(context.start())
app = create_app(context=context, docs_enabled=False)
from starlette.requests import Request
from pyfly.web.adapters.starlette.html_errors import render_html_error
request = Request({'type': 'http', 'app': app, 'method': 'GET', 'scheme': 'http',
                   'path': '/missing', 'root_path': '', 'query_string': b'',
                   'headers': [(b'accept', b'text/html')], 'server': ('test', 80)})
response = asyncio.run(render_html_error(request, 404, 'Not found'))
assert response.status_code == 404 and b'PyFly' in response.body
asyncio.run(context.stop())
"""
    if "data-relational" in extra:
        script += """
from pyfly.admin.data.adapters.sqlalchemy import SqlAlchemyAdminProvider
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
class Base(DeclarativeBase): pass
class Item(Base):
    __tablename__ = 'items'
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]
from pyfly.admin.data.models import AdminOperationContext
from pyfly.security.context import SecurityContext
async def exercise():
    sql = create_async_engine('sqlite+aiosqlite:///installed.db')
    async with sql.begin() as conn: await conn.run_sync(Base.metadata.create_all)
    provider = SqlAlchemyAdminProvider(async_sessionmaker(sql), edit_token_key='installed-wheel-secret-key-32bytes')
    resource = ModelAdmin('items', Item, fields=('id', 'title'), editable_fields=('title',))
    actor = AdminOperationContext(SecurityContext(user_id='admin', roles=['ADMIN']))
    saved = await provider.create(resource, {'title': 'Installed'}, actor)
    assert saved.values['title'] == 'Installed'
    await sql.dispose()
asyncio.run(exercise())
"""
    if "data-document" in extra:
        script += """
from pyfly.admin.data.adapters.beanie import BeanieAdminProvider
from pyfly.data.document.mongodb.document import BaseDocument
class Article(BaseDocument): title: str
schema = BeanieAdminProvider().describe(ModelAdmin('articles', Article, fields=('id', 'title')))
assert len(schema) == 2
"""
    result = subprocess.run([str(python), "-I", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
