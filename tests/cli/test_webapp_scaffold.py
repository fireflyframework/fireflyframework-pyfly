"""Run generated code in a fresh process using framework-owned templates/static mounts."""

import os
import subprocess
import sys

import pytest

from pyfly.cli.templates import generate_project


def test_generated_webapp(tmp_path):
    generate_project("catalog", tmp_path, "web", ["web"])
    main = (tmp_path / "src/catalog/main.py").read_text()
    assert "StaticFiles" not in main
    controller = (tmp_path / "src/catalog/controllers/home_controller.py").read_text()
    assert "ModelAndView" in controller
    script = """
from starlette.testclient import TestClient
from catalog.main import app
with TestClient(app) as client:
    for path, expected in [('/', 'Welcome to catalog'), ('/about', 'About catalog')]:
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert 'text/html' in response.headers['content-type']
        assert expected in response.text
    assert client.get('/static/css/style.css').status_code == 200
    assert client.get('/_pyfly/web/logo.png').status_code == 200
    assert '/_pyfly/web/web.css' in client.get('/').text
"""
    result = subprocess.run(
        [sys.executable, "-u", "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(tmp_path / "src"),
            "PYFLY_LOGGING_REDACTION_ENGINE": "regex",
            "PYFLY_MANAGEMENT_SERVER_PORT": "8080",
        },
        stdout=(tmp_path / "scaffold.log").open("w"),
        stderr=subprocess.STDOUT,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, (tmp_path / "scaffold.log").read_text()


@pytest.mark.parametrize("archetype", ["web-api", "fastapi-api", "hexagonal", "core", "library", "cli"])
def test_service_archetypes_do_not_enable_browser_features(tmp_path, archetype):
    from pyfly.core.config import Config
    from pyfly.web.templating.config import HtmlErrorProperties, StaticProperties, TemplateProperties

    generate_project("service", tmp_path, archetype, ["web"])
    config = Config.from_file(tmp_path / "pyfly.yaml")
    assert not config.bind(TemplateProperties).enabled
    assert not config.bind(StaticProperties).enabled
    assert not config.bind(HtmlErrorProperties).html_enabled
