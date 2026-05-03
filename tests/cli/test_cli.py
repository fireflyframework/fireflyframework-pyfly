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
"""Tests for CLI commands."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from pyfly.cli.main import cli


class TestCLI:
    def test_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "PyFly" in result.output

    def test_help_shows_copyright(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Copyright 2026 Firefly Software Foundation." in result.output
        assert "Apache 2.0 License" in result.output

    def test_help_shows_aligned_tagline(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "official Python implementation of the Firefly Framework" in result.output

    def test_new_command_creates_project(self, tmp_path: Path):
        runner = CliRunner()
        project_name = "order-service"
        result = runner.invoke(cli, ["new", project_name, "--directory", str(tmp_path)])
        assert result.exit_code == 0, result.output

        project_dir = tmp_path / project_name
        assert project_dir.exists()
        assert (project_dir / "pyproject.toml").exists()
        assert (project_dir / "src" / "order_service" / "__init__.py").exists()
        assert (project_dir / "tests" / "conftest.py").exists()
        assert (project_dir / "pyfly.yaml").exists()

    def test_new_command_library_archetype(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["new", "common-lib", "--archetype", "library", "--directory", str(tmp_path)])
        assert result.exit_code == 0, result.output

        project_dir = tmp_path / "common-lib"
        assert project_dir.exists()
        assert (project_dir / "pyproject.toml").exists()
        assert not (project_dir / "pyfly.yaml").exists()

    def test_new_command_pyproject_content(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["new", "my-service", "--directory", str(tmp_path)])
        assert result.exit_code == 0, result.output

        content = (tmp_path / "my-service" / "pyproject.toml").read_text()
        assert 'name = "my-service"' in content
        assert "pyfly" in content

    def test_new_command_with_existing_dir_fails(self, tmp_path: Path):
        runner = CliRunner()
        (tmp_path / "existing").mkdir()
        result = runner.invoke(cli, ["new", "existing", "--directory", str(tmp_path)])
        assert result.exit_code != 0


class TestNewWebApi:
    """Tests for the web-api archetype."""

    def test_web_api_creates_full_structure(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "item-api",
                "--archetype",
                "web-api",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        p = tmp_path / "item-api"
        pkg = p / "src" / "item_api"

        # Core files
        assert (p / "pyproject.toml").exists()
        assert (p / "pyfly.yaml").exists()
        assert (p / "Dockerfile").exists()
        assert (p / "README.md").exists()
        assert (p / ".gitignore").exists()
        assert (p / ".env.example").exists()

        # Layered package structure
        assert (pkg / "app.py").exists()
        assert (pkg / "controllers" / "health_controller.py").exists()
        assert (pkg / "controllers" / "todo_controller.py").exists()
        assert (pkg / "services" / "todo_service.py").exists()
        assert (pkg / "models" / "todo.py").exists()
        assert (pkg / "repositories" / "todo_repository.py").exists()

        # Test file
        assert (p / "tests" / "test_todo_service.py").exists()

    def test_web_api_controller_uses_stereotypes(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-api",
                "--archetype",
                "web-api",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        controller = (tmp_path / "my-api" / "src" / "my_api" / "controllers" / "todo_controller.py").read_text()
        assert "@rest_controller" in controller
        assert "@request_mapping" in controller
        assert "@get_mapping" in controller
        assert "@post_mapping" in controller
        assert "from pyfly.container import rest_controller" in controller

    def test_web_api_service_uses_stereotype(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-api",
                "--archetype",
                "web-api",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        service = (tmp_path / "my-api" / "src" / "my_api" / "services" / "todo_service.py").read_text()
        assert "@service" in service
        assert "from pyfly.container import service" in service

    def test_web_api_repository_uses_stereotype(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-api",
                "--archetype",
                "web-api",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        repo = (tmp_path / "my-api" / "src" / "my_api" / "repositories" / "todo_repository.py").read_text()
        assert "@repository" in repo
        assert "from pyfly.container import repository" in repo

    def test_web_api_app_scan_packages(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-api",
                "--archetype",
                "web-api",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        app = (tmp_path / "my-api" / "src" / "my_api" / "app.py").read_text()
        assert "my_api.controllers" in app
        assert "my_api.services" in app
        assert "my_api.repositories" in app

    def test_web_api_default_features_web(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-api",
                "--archetype",
                "web-api",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        pyproject = (tmp_path / "my-api" / "pyproject.toml").read_text()
        assert "pyfly[web]" in pyproject


class TestNewHexagonal:
    """Tests for the hexagonal archetype."""

    def test_hexagonal_creates_full_structure(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "order-svc",
                "--archetype",
                "hexagonal",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        p = tmp_path / "order-svc"
        pkg = p / "src" / "order_svc"

        # Core files
        assert (p / "pyproject.toml").exists()
        assert (p / "pyfly.yaml").exists()
        assert (p / "Dockerfile").exists()
        assert (p / "README.md").exists()

        # Hexagonal layers
        assert (pkg / "domain" / "models.py").exists()
        assert (pkg / "domain" / "events.py").exists()
        assert (pkg / "domain" / "ports" / "inbound.py").exists()
        assert (pkg / "domain" / "ports" / "outbound.py").exists()
        assert (pkg / "application" / "services.py").exists()
        assert (pkg / "infrastructure" / "adapters" / "persistence.py").exists()
        assert (pkg / "infrastructure" / "config.py").exists()
        assert (pkg / "api" / "controllers.py").exists()
        assert (pkg / "api" / "dto.py").exists()

        # Tests by layer
        assert (p / "tests" / "domain" / "test_models.py").exists()
        assert (p / "tests" / "application" / "test_services.py").exists()

    def test_hexagonal_uses_protocol_ports(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-hex",
                "--archetype",
                "hexagonal",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        inbound = (tmp_path / "my-hex" / "src" / "my_hex" / "domain" / "ports" / "inbound.py").read_text()
        assert "Protocol" in inbound
        assert "class CreateTodoUseCase" in inbound

    def test_hexagonal_app_scan_packages(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-hex",
                "--archetype",
                "hexagonal",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        app = (tmp_path / "my-hex" / "src" / "my_hex" / "app.py").read_text()
        assert "my_hex.api" in app
        assert "my_hex.application" in app
        assert "my_hex.infrastructure" in app


class TestNewWeb:
    """Tests for the web (SSR) archetype."""

    def test_web_creates_full_structure(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-site",
                "--archetype",
                "web",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        p = tmp_path / "my-site"
        pkg = p / "src" / "my_site"

        # Core files
        assert (p / "pyproject.toml").exists()
        assert (p / "pyfly.yaml").exists()
        assert (p / "Dockerfile").exists()
        assert (p / "README.md").exists()
        assert (p / ".gitignore").exists()
        assert (p / ".env.example").exists()

        # Controllers
        assert (pkg / "controllers" / "health_controller.py").exists()
        assert (pkg / "controllers" / "home_controller.py").exists()

        # Services
        assert (pkg / "services" / "page_service.py").exists()

        # Templates & Static
        assert (pkg / "templates" / "base.html").exists()
        assert (pkg / "templates" / "home.html").exists()
        assert (pkg / "templates" / "about.html").exists()
        assert (pkg / "static" / "css" / "style.css").exists()

        # Test file
        assert (p / "tests" / "test_home_controller.py").exists()

    def test_web_controller_uses_controller_stereotype(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-site",
                "--archetype",
                "web",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        controller = (tmp_path / "my-site" / "src" / "my_site" / "controllers" / "home_controller.py").read_text()
        assert "@controller" in controller
        assert "from pyfly.container import controller" in controller

    def test_web_has_template_response(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-site",
                "--archetype",
                "web",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        controller = (tmp_path / "my-site" / "src" / "my_site" / "controllers" / "home_controller.py").read_text()
        assert "TemplateResponse" in controller

    def test_web_has_html_templates(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-site",
                "--archetype",
                "web",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        pkg = tmp_path / "my-site" / "src" / "my_site"
        base = (pkg / "templates" / "base.html").read_text()
        assert "<!DOCTYPE html>" in base

        home = (pkg / "templates" / "home.html").read_text()
        assert "extends" in home

        about = (pkg / "templates" / "about.html").read_text()
        assert "extends" in about

    def test_web_has_static_css(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-site",
                "--archetype",
                "web",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        css = (tmp_path / "my-site" / "src" / "my_site" / "static" / "css" / "style.css").read_text()
        assert "body" in css

    def test_web_service_uses_stereotype(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-site",
                "--archetype",
                "web",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        service = (tmp_path / "my-site" / "src" / "my_site" / "services" / "page_service.py").read_text()
        assert "@service" in service
        assert "from pyfly.container import service" in service

    def test_web_app_scan_packages(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-site",
                "--archetype",
                "web",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        app = (tmp_path / "my-site" / "src" / "my_site" / "app.py").read_text()
        assert "my_site.controllers" in app
        assert "my_site.services" in app

    def test_web_default_features_web(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-site",
                "--archetype",
                "web",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        pyproject = (tmp_path / "my-site" / "pyproject.toml").read_text()
        assert "pyfly[web]" in pyproject

    def test_web_has_jinja2_dependency(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-site",
                "--archetype",
                "web",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        pyproject = (tmp_path / "my-site" / "pyproject.toml").read_text()
        assert "jinja2" in pyproject

    def test_web_main_mounts_static(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-site",
                "--archetype",
                "web",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        main = (tmp_path / "my-site" / "src" / "my_site" / "main.py").read_text()
        assert "StaticFiles" in main
        assert "/static" in main

    def test_web_readme_mentions_html(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-site",
                "--archetype",
                "web",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        readme = (tmp_path / "my-site" / "README.md").read_text()
        assert "HTML" in readme or "template" in readme.lower()

    def test_web_pyproject_description(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-site",
                "--archetype",
                "web",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        pyproject = (tmp_path / "my-site" / "pyproject.toml").read_text()
        assert "web application" in pyproject

    def test_web_config_has_web_section(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-site",
                "--archetype",
                "web",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        config = (tmp_path / "my-site" / "pyfly.yaml").read_text()
        assert "port: 8080" in config


class TestNewFeatures:
    """Tests for the --features flag."""

    def test_features_flag_sets_dependencies(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-svc",
                "--features",
                "web,data-relational,cache",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        pyproject = (tmp_path / "my-svc" / "pyproject.toml").read_text()
        assert "pyfly[web,data-relational,cache]" in pyproject

    def test_features_affect_config(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-svc",
                "--features",
                "web,data-relational",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        config = (tmp_path / "my-svc" / "pyfly.yaml").read_text()
        assert "data:" in config
        assert "relational:" in config
        assert "sqlite+aiosqlite://" in config
        assert "port: 8080" in config

    def test_features_data_adds_database_url_to_env(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-svc",
                "--features",
                "data-relational",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        env = (tmp_path / "my-svc" / ".env.example").read_text()
        assert "PYFLY_DATA_RELATIONAL_URL" in env
        assert "sqlite+aiosqlite://" in env

    def test_features_eda_defaults_to_memory(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-svc",
                "--features",
                "eda",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        config = (tmp_path / "my-svc" / "pyfly.yaml").read_text()
        assert "messaging:" in config
        assert "provider: memory" in config

    def test_features_cache_defaults_to_memory(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-svc",
                "--features",
                "cache",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        config = (tmp_path / "my-svc" / "pyfly.yaml").read_text()
        assert "cache:" in config
        assert "enabled: true" in config
        assert "provider: memory" in config

    def test_invalid_feature_fails(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-svc",
                "--features",
                "web,nosuchfeature",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code != 0

    def test_shell_feature_adds_dependency(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-svc",
                "--features",
                "shell",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        pyproject = (tmp_path / "my-svc" / "pyproject.toml").read_text()
        assert "pyfly[shell]" in pyproject

    def test_shell_feature_adds_config(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-svc",
                "--features",
                "shell",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        config = (tmp_path / "my-svc" / "pyfly.yaml").read_text()
        assert "shell:" in config
        assert "enabled: true" in config

    def test_no_features_for_library(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-lib",
                "--archetype",
                "library",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        pyproject = (tmp_path / "my-lib" / "pyproject.toml").read_text()
        # Library with no features should have bare pyfly dependency
        assert '"pyfly"' in pyproject


class TestConfigKeyAlignment:
    """Scaffolded config must use keys that auto-config actually reads."""

    def test_eda_uses_messaging_provider_key(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-svc",
                "--features",
                "eda",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        config = (tmp_path / "my-svc" / "pyfly.yaml").read_text()
        assert "messaging:" in config
        assert "provider: memory" in config
        assert "eda:" not in config  # Must NOT use old wrong key

    def test_cache_uses_enabled_and_provider_keys(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-svc",
                "--features",
                "cache",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        config = (tmp_path / "my-svc" / "pyfly.yaml").read_text()
        assert "cache:" in config
        assert "enabled: true" in config
        assert "provider: memory" in config

    def test_env_example_uses_messaging_key(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-svc",
                "--features",
                "eda",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        env = (tmp_path / "my-svc" / ".env.example").read_text()
        assert "PYFLY_MESSAGING_PROVIDER" in env


class TestNewEnhancedCore:
    """Tests for enhanced core archetype."""

    def test_core_has_dockerfile(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-svc",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / "my-svc" / "Dockerfile").exists()

    def test_core_has_readme(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-svc",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / "my-svc" / "README.md").exists()

    def test_core_has_env_example(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-svc",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / "my-svc" / ".env.example").exists()

    def test_core_dockerfile_multi_stage(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-svc",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        dockerfile = (tmp_path / "my-svc" / "Dockerfile").read_text()
        assert "FROM python:3.12-slim AS builder" in dockerfile
        assert "FROM python:3.12-slim" in dockerfile
        assert "EXPOSE 8080" in dockerfile


class TestNewEnhancedLibrary:
    """Tests for enhanced library archetype."""

    def test_library_has_py_typed(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-lib",
                "--archetype",
                "library",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / "my-lib" / "src" / "my_lib" / "py.typed").exists()

    def test_library_has_readme(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-lib",
                "--archetype",
                "library",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        readme = (tmp_path / "my-lib" / "README.md").read_text()
        assert "my-lib" in readme
        assert "library" in readme.lower()

    def test_library_no_dockerfile(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-lib",
                "--archetype",
                "library",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        # Libraries don't need a Dockerfile
        assert not (tmp_path / "my-lib" / "Dockerfile").exists()

    def test_library_no_pyfly_yaml(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-lib",
                "--archetype",
                "library",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert not (tmp_path / "my-lib" / "pyfly.yaml").exists()


class TestNewInteractive:
    """Tests for interactive mode (no NAME argument)."""

    @patch("pyfly.cli.new.questionary")
    def test_interactive_creates_project(self, mock_q, tmp_path: Path):
        # Mock questionary responses
        mock_q.text.return_value.unsafe_ask.side_effect = ["demo-svc", "demo_svc"]
        mock_q.select.return_value.unsafe_ask.return_value = "core"
        mock_q.checkbox.return_value.unsafe_ask.return_value = []
        mock_q.confirm.return_value.unsafe_ask.return_value = True

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["new", "--directory", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / "demo-svc").exists()
        assert (tmp_path / "demo-svc" / "pyproject.toml").exists()

    @patch("pyfly.cli.new.questionary")
    def test_interactive_web_api(self, mock_q, tmp_path: Path):
        mock_q.text.return_value.unsafe_ask.side_effect = ["my-api", "my_api"]
        mock_q.select.return_value.unsafe_ask.return_value = "web-api"
        mock_q.checkbox.return_value.unsafe_ask.return_value = ["web"]
        mock_q.confirm.return_value.unsafe_ask.return_value = True

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["new", "--directory", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output

        p = tmp_path / "my-api"
        assert (p / "src" / "my_api" / "controllers" / "todo_controller.py").exists()

    @patch("pyfly.cli.new.questionary")
    def test_interactive_web(self, mock_q, tmp_path: Path):
        mock_q.text.return_value.unsafe_ask.side_effect = ["my-site", "my_site"]
        mock_q.select.return_value.unsafe_ask.return_value = "web"
        mock_q.checkbox.return_value.unsafe_ask.return_value = ["web"]
        mock_q.confirm.return_value.unsafe_ask.return_value = True

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["new", "--directory", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output

        p = tmp_path / "my-site"
        assert (p / "src" / "my_site" / "controllers" / "home_controller.py").exists()
        assert (p / "src" / "my_site" / "templates" / "base.html").exists()
        assert (p / "src" / "my_site" / "static" / "css" / "style.css").exists()

    @patch("pyfly.cli.new.questionary")
    def test_interactive_cancel(self, mock_q, tmp_path: Path):
        mock_q.text.return_value.unsafe_ask.side_effect = ["cancel-me", "cancel_me"]
        mock_q.select.return_value.unsafe_ask.return_value = "core"
        mock_q.checkbox.return_value.unsafe_ask.return_value = []
        mock_q.confirm.return_value.unsafe_ask.return_value = False

        runner = CliRunner()
        _result = runner.invoke(
            cli,
            ["new", "--directory", str(tmp_path)],
        )
        # Confirm=False raises SystemExit(0), which CliRunner captures
        assert not (tmp_path / "cancel-me").exists()

    @patch("pyfly.cli.new.questionary")
    def test_interactive_keyboard_interrupt(self, mock_q, tmp_path: Path):
        mock_q.text.return_value.unsafe_ask.side_effect = KeyboardInterrupt()

        runner = CliRunner()
        _result = runner.invoke(
            cli,
            ["new", "--directory", str(tmp_path)],
        )
        # Should exit cleanly without traceback
        assert not (tmp_path / "any-project").exists()

    @patch("pyfly.cli.new.questionary")
    def test_interactive_library_skips_features(self, mock_q, tmp_path: Path):
        mock_q.text.return_value.unsafe_ask.side_effect = ["my-lib", "my_lib"]
        mock_q.select.return_value.unsafe_ask.return_value = "library"
        mock_q.confirm.return_value.unsafe_ask.return_value = True

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["new", "--directory", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / "my-lib").exists()
        # checkbox should NOT have been called for library
        mock_q.checkbox.assert_not_called()

    @patch("pyfly.cli.new.questionary")
    def test_interactive_cli(self, mock_q, tmp_path: Path):
        mock_q.text.return_value.unsafe_ask.side_effect = ["my-cli", "my_cli"]
        mock_q.select.return_value.unsafe_ask.return_value = "cli"
        mock_q.checkbox.return_value.unsafe_ask.return_value = ["shell"]
        mock_q.confirm.return_value.unsafe_ask.return_value = True

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["new", "--directory", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output

        p = tmp_path / "my-cli"
        assert (p / "src" / "my_cli" / "commands" / "hello_command.py").exists()


class TestNameValidation:
    """Tests for project name validation."""

    def test_reserved_name_test_rejected(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["new", "test", "--directory", str(tmp_path)])
        assert result.exit_code != 0
        assert "conflicts" in result.output

    def test_reserved_name_os_rejected(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["new", "os", "--directory", str(tmp_path)])
        assert result.exit_code != 0

    def test_reserved_name_sys_rejected(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["new", "sys", "--directory", str(tmp_path)])
        assert result.exit_code != 0

    def test_python_keyword_rejected(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["new", "class", "--directory", str(tmp_path)])
        assert result.exit_code != 0
        assert "keyword" in result.output

    def test_name_starting_with_digit_rejected(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["new", "123-service", "--directory", str(tmp_path)])
        assert result.exit_code != 0

    def test_valid_name_accepted(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["new", "order-service", "--directory", str(tmp_path)])
        assert result.exit_code == 0

    def test_postgresql_not_a_valid_feature(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-svc",
                "--features",
                "web,postgresql",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code != 0
        assert "Unknown features" in result.output


class TestDockerfile:
    """Tests for generated Dockerfile content."""

    def test_dockerfile_uses_uvicorn_directly(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["new", "my-svc", "--directory", str(tmp_path)])
        assert result.exit_code == 0, result.output

        dockerfile = (tmp_path / "my-svc" / "Dockerfile").read_text()
        assert "uvicorn" in dockerfile
        assert "my_svc.main:app" in dockerfile
        assert "pyfly run" not in dockerfile

    def test_dockerfile_copies_pyfly_yaml(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["new", "my-svc", "--directory", str(tmp_path)])
        assert result.exit_code == 0, result.output

        dockerfile = (tmp_path / "my-svc" / "Dockerfile").read_text()
        assert "COPY pyfly.yaml" in dockerfile


class TestGitignore:
    """Tests for generated .gitignore content."""

    def test_gitignore_includes_db_files(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["new", "my-svc", "--directory", str(tmp_path)])
        assert result.exit_code == 0, result.output

        gitignore = (tmp_path / "my-svc" / ".gitignore").read_text()
        assert "*.db" in gitignore


class TestPostGenerationGuidance:
    """Tests for post-generation next-steps output."""

    def test_shows_next_steps(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["new", "my-svc", "--directory", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "Next steps" in result.output
        assert "uv sync" in result.output
        assert "pyfly run" in result.output

    def test_data_feature_shows_tips(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-svc",
                "--features",
                "data-relational",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "pyfly db init" in result.output

    def test_web_feature_shows_swagger_tip(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-svc",
                "--features",
                "web",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Swagger UI" in result.output

    def test_security_feature_shows_jwt_tip(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-svc",
                "--features",
                "security",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "JWT secret" in result.output


class TestTemplateContent:
    """Tests that scaffolded files contain modern features (Valid[T], Field, actuator, etc.)."""

    def test_web_api_controller_uses_valid(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-api",
                "--archetype",
                "web-api",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        controller = (tmp_path / "my-api" / "src" / "my_api" / "controllers" / "todo_controller.py").read_text()
        assert "Valid[TodoCreate]" in controller
        assert "Body[TodoCreate]" not in controller

    def test_model_uses_field_constraints(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-api",
                "--archetype",
                "web-api",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        model = (tmp_path / "my-api" / "src" / "my_api" / "models" / "todo.py").read_text()
        assert "Field(min_length=" in model
        assert "from pydantic import BaseModel, Field" in model

    def test_config_has_adapter_auto(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-api",
                "--archetype",
                "web-api",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        config = (tmp_path / "my-api" / "pyfly.yaml").read_text()
        assert "adapter: auto" in config

    def test_config_has_actuator_enabled(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-api",
                "--archetype",
                "web-api",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        config = (tmp_path / "my-api" / "pyfly.yaml").read_text()
        assert "actuator:" in config
        assert "enabled: true" in config

    def test_data_feature_generates_sqlalchemy_repo(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-api",
                "--archetype",
                "web-api",
                "--features",
                "web,data-relational",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        repo = (tmp_path / "my-api" / "src" / "my_api" / "repositories" / "todo_repository.py").read_text()
        assert "Repository[TodoEntity, int]" in repo
        assert "from pyfly.data.relational.sqlalchemy import Repository" in repo

    def test_no_data_feature_generates_in_memory_repo(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-api",
                "--archetype",
                "web-api",
                "--features",
                "web",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        repo = (tmp_path / "my-api" / "src" / "my_api" / "repositories" / "todo_repository.py").read_text()
        assert "dict[str, TodoResponse]" in repo
        assert "Repository[TodoEntity" not in repo

    def test_data_feature_generates_entity_in_model(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-api",
                "--archetype",
                "web-api",
                "--features",
                "web,data-relational",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        model = (tmp_path / "my-api" / "src" / "my_api" / "models" / "todo.py").read_text()
        assert "class TodoEntity(Base):" in model
        assert "id: Mapped[int]" in model

    def test_no_data_feature_no_entity(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-api",
                "--archetype",
                "web-api",
                "--features",
                "web",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        model = (tmp_path / "my-api" / "src" / "my_api" / "models" / "todo.py").read_text()
        assert "class TodoEntity" not in model
        assert "id: str" in model

    def test_data_feature_controller_uses_int_path_var(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-api",
                "--archetype",
                "web-api",
                "--features",
                "web,data-relational",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        controller = (tmp_path / "my-api" / "src" / "my_api" / "controllers" / "todo_controller.py").read_text()
        assert "PathVar[int]" in controller

    def test_hexagonal_controller_uses_valid(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-hex",
                "--archetype",
                "hexagonal",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        controller = (tmp_path / "my-hex" / "src" / "my_hex" / "api" / "controllers.py").read_text()
        assert "Valid[TodoCreateRequest]" in controller
        assert "Body[TodoCreateRequest]" not in controller

    def test_hexagonal_dto_uses_field(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-hex",
                "--archetype",
                "hexagonal",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        dto = (tmp_path / "my-hex" / "src" / "my_hex" / "api" / "dto.py").read_text()
        assert "Field(min_length=" in dto


class TestMongoDBFeature:
    """Tests for the mongodb feature scaffolding."""

    def test_mongodb_feature_generates_document_model(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-api",
                "--archetype",
                "web-api",
                "--features",
                "web,data-document",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        model = (tmp_path / "my-api" / "src" / "my_api" / "models" / "todo.py").read_text()
        assert "class TodoDocument(BaseDocument):" in model
        assert "from pyfly.data.document.mongodb import BaseDocument" in model

    def test_mongodb_feature_generates_mongo_repo(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-api",
                "--archetype",
                "web-api",
                "--features",
                "web,data-document",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        repo = (tmp_path / "my-api" / "src" / "my_api" / "repositories" / "todo_repository.py").read_text()
        assert "MongoRepository[TodoDocument, str]" in repo
        assert "from pyfly.data.document.mongodb import MongoRepository" in repo

    def test_mongodb_config_in_yaml(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-api",
                "--features",
                "data-document",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        config = (tmp_path / "my-api" / "pyfly.yaml").read_text()
        assert "document:" in config
        assert "uri: mongodb://localhost:27017" in config
        assert "database: my_api" in config

    def test_mongodb_env_example(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-api",
                "--features",
                "data-document",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        env = (tmp_path / "my-api" / ".env.example").read_text()
        assert "PYFLY_DATA_DOCUMENT_URI" in env

    def test_mongodb_feature_sets_dependencies(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-svc",
                "--features",
                "web,data-document",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        pyproject = (tmp_path / "my-svc" / "pyproject.toml").read_text()
        assert "pyfly[web,data-document]" in pyproject

    def test_mongodb_shows_tips(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-svc",
                "--features",
                "data-document",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Beanie ODM" in result.output

    def test_hexagonal_mongodb_persistence(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-hex",
                "--archetype",
                "hexagonal",
                "--features",
                "web,data-document",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        persistence = (
            tmp_path / "my-hex" / "src" / "my_hex" / "infrastructure" / "adapters" / "persistence.py"
        ).read_text()
        assert "MongoRepository[TodoDocument, str]" in persistence

    def test_hexagonal_mongodb_domain_model(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-hex",
                "--archetype",
                "hexagonal",
                "--features",
                "web,data-document",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        models = (tmp_path / "my-hex" / "src" / "my_hex" / "domain" / "models.py").read_text()
        assert "class TodoDocument(BaseDocument):" in models

    def test_mongodb_controller_uses_str_path_var(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-api",
                "--archetype",
                "web-api",
                "--features",
                "web,data-document",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        controller = (tmp_path / "my-api" / "src" / "my_api" / "controllers" / "todo_controller.py").read_text()
        assert "PathVar[str]" in controller

    def test_mongodb_service_is_async(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-api",
                "--archetype",
                "web-api",
                "--features",
                "web,data-document",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        service = (tmp_path / "my-api" / "src" / "my_api" / "services" / "todo_service.py").read_text()
        assert "async def create" in service
        assert "await self._repository" in service

    def test_both_data_and_mongodb_generates_both(self, tmp_path: Path):
        """When both data-relational and data-document features are selected, both backends are scaffolded."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-api",
                "--archetype",
                "web-api",
                "--features",
                "web,data-relational,data-document",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        model = (tmp_path / "my-api" / "src" / "my_api" / "models" / "todo.py").read_text()
        assert "class TodoEntity(Base):" in model
        assert "class TodoDocument(BaseDocument):" in model

        repo = (tmp_path / "my-api" / "src" / "my_api" / "repositories" / "todo_repository.py").read_text()
        assert "Repository[TodoEntity, int]" in repo
        assert "MongoRepository[TodoDocument, str]" in repo


class TestLicenseCommand:
    """Tests for the pyfly license command."""

    def test_license_runs(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["license"])
        assert result.exit_code == 0
        assert "Apache License" in result.output

    def test_license_shows_copyright(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["license"])
        assert result.exit_code == 0
        assert "Copyright 2026 Firefly Software Foundation." in result.output

    def test_license_shows_full_text(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["license"])
        assert result.exit_code == 0
        assert "TERMS AND CONDITIONS" in result.output


class TestSbomCommand:
    """Tests for the pyfly sbom command."""

    def test_sbom_runs(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["sbom"])
        assert result.exit_code == 0
        assert "Software Bill of Materials" in result.output

    def test_sbom_shows_dependencies(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["sbom"])
        assert result.exit_code == 0
        # pydantic and pyyaml are core dependencies
        assert "pydantic" in result.output
        assert "pyyaml" in result.output.lower() or "PyYAML" in result.output

    def test_sbom_shows_total_count(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["sbom"])
        assert result.exit_code == 0
        assert "Total dependencies:" in result.output

    def test_sbom_json_output(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["sbom", "--json"])
        assert result.exit_code == 0
        assert '"name": "pyfly"' in result.output
        assert '"dependencies"' in result.output


class TestInfoCommand:
    def test_info_shows_version(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["info"])
        assert result.exit_code == 0
        assert "PyFly Framework" in result.output
        assert "0.2.0" in result.output

    def test_info_shows_python_version(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["info"])
        assert result.exit_code == 0
        assert "Python" in result.output

    def test_info_shows_extras(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["info"])
        assert result.exit_code == 0
        assert "Installed Extras" in result.output
        assert "web" in result.output


class TestRunCommand:
    def test_run_without_app_or_config_fails(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["run"], catch_exceptions=False)
        assert result.exit_code != 0
        assert "No application found" in result.output

    def test_run_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--help"])
        assert result.exit_code == 0
        assert "--host" in result.output
        assert "--port" in result.output
        assert "--reload" in result.output

    def test_run_discovers_app_from_pyfly_yaml(self, tmp_path: Path, monkeypatch: object):
        """pyfly run should discover the module from pyfly.yaml."""
        monkeypatch.chdir(tmp_path)

        (tmp_path / "pyfly.yaml").write_text("pyfly:\n  app:\n    name: my-test\n    module: my_test.main:app\n")

        from pyfly.cli.run import _discover_app

        assert _discover_app() == "my_test.main:app"

    def test_run_auto_discovers_from_src(self, tmp_path: Path, monkeypatch: object):
        """pyfly run should auto-discover main.py in src/ when module is not in yaml."""
        monkeypatch.chdir(tmp_path)

        (tmp_path / "pyfly.yaml").write_text("pyfly:\n  app:\n    name: my-test\n")
        (tmp_path / "src" / "my_test").mkdir(parents=True)
        (tmp_path / "src" / "my_test" / "main.py").write_text("app = None\n")

        from pyfly.cli.run import _discover_app

        assert _discover_app() == "my_test.main:app"


class TestMainPyGeneration:
    """Tests for main.py ASGI entry point generation."""

    def test_core_generates_main_py(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["new", "my-svc", "--directory", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "my-svc" / "src" / "my_svc" / "main.py").exists()

    def test_web_api_generates_main_py(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-api",
                "--archetype",
                "web-api",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / "my-api" / "src" / "my_api" / "main.py").exists()

    def test_hexagonal_generates_main_py(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-hex",
                "--archetype",
                "hexagonal",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / "my-hex" / "src" / "my_hex" / "main.py").exists()

    def test_library_does_not_generate_main_py(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-lib",
                "--archetype",
                "library",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert not (tmp_path / "my-lib" / "src" / "my_lib" / "main.py").exists()

    def test_main_py_imports_application(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["new", "my-svc", "--directory", str(tmp_path)])
        assert result.exit_code == 0, result.output

        main = (tmp_path / "my-svc" / "src" / "my_svc" / "main.py").read_text()
        assert "from my_svc.app import Application" in main
        assert "PyFlyApplication" in main
        assert "create_app" in main

    def test_main_py_has_lifespan(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["new", "my-svc", "--directory", str(tmp_path)])
        assert result.exit_code == 0, result.output

        main = (tmp_path / "my-svc" / "src" / "my_svc" / "main.py").read_text()
        assert "_lifespan" in main
        assert "startup" in main
        assert "shutdown" in main

    def test_pyfly_yaml_has_module_field(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["new", "my-svc", "--directory", str(tmp_path)])
        assert result.exit_code == 0, result.output

        config = (tmp_path / "my-svc" / "pyfly.yaml").read_text()
        assert "module: my_svc.main:app" in config

    def test_library_yaml_has_no_module_field(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-lib",
                "--archetype",
                "library",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        # Libraries don't have pyfly.yaml at all
        assert not (tmp_path / "my-lib" / "pyfly.yaml").exists()


class TestDoctorCommand:
    def test_doctor_runs(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        assert result.exit_code == 0
        assert "PyFly Doctor" in result.output

    def test_doctor_checks_python(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        assert result.exit_code == 0
        assert "Python" in result.output

    def test_doctor_checks_tools(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        assert result.exit_code == 0
        # git should be found in any dev environment
        assert "git" in result.output


class TestNewCli:
    """Tests for the cli archetype."""

    def test_cli_creates_full_structure(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-tool",
                "--archetype",
                "cli",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        p = tmp_path / "my-tool"
        pkg = p / "src" / "my_tool"

        # Core files
        assert (p / "pyproject.toml").exists()
        assert (p / "pyfly.yaml").exists()
        assert (p / "Dockerfile").exists()
        assert (p / "README.md").exists()
        assert (p / ".gitignore").exists()
        assert (p / ".env.example").exists()

        # CLI package structure
        assert (pkg / "app.py").exists()
        assert (pkg / "main.py").exists()
        assert (pkg / "commands" / "__init__.py").exists()
        assert (pkg / "commands" / "hello_command.py").exists()
        assert (pkg / "services" / "__init__.py").exists()
        assert (pkg / "services" / "greeting_service.py").exists()

        # Test file
        assert (p / "tests" / "test_hello_command.py").exists()

    def test_cli_has_shell_component(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-tool",
                "--archetype",
                "cli",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        command = (tmp_path / "my-tool" / "src" / "my_tool" / "commands" / "hello_command.py").read_text()
        assert "@shell_component" in command

    def test_cli_has_shell_method(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-tool",
                "--archetype",
                "cli",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        command = (tmp_path / "my-tool" / "src" / "my_tool" / "commands" / "hello_command.py").read_text()
        assert "@shell_method" in command

    def test_cli_service_uses_stereotype(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-tool",
                "--archetype",
                "cli",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        service = (tmp_path / "my-tool" / "src" / "my_tool" / "services" / "greeting_service.py").read_text()
        assert "@service" in service
        assert "from pyfly.container import service" in service

    def test_cli_app_scan_packages(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-tool",
                "--archetype",
                "cli",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        app = (tmp_path / "my-tool" / "src" / "my_tool" / "app.py").read_text()
        assert "my_tool.commands" in app
        assert "my_tool.services" in app

    def test_cli_default_features_shell(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-tool",
                "--archetype",
                "cli",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        pyproject = (tmp_path / "my-tool" / "pyproject.toml").read_text()
        assert "pyfly[shell]" in pyproject

    def test_cli_config_has_shell_enabled(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-tool",
                "--archetype",
                "cli",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        config = (tmp_path / "my-tool" / "pyfly.yaml").read_text()
        assert "shell:" in config
        assert "enabled: true" in config

    def test_cli_main_is_not_asgi(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-tool",
                "--archetype",
                "cli",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        main = (tmp_path / "my-tool" / "src" / "my_tool" / "main.py").read_text()
        assert "create_app" not in main
        assert "uvicorn" not in main
        assert "asyncio.run" in main

    def test_cli_dockerfile_no_expose(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-tool",
                "--archetype",
                "cli",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        dockerfile = (tmp_path / "my-tool" / "Dockerfile").read_text()
        assert "EXPOSE" not in dockerfile
        assert "my_tool.main" in dockerfile

    def test_cli_readme_mentions_cli(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-tool",
                "--archetype",
                "cli",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        readme = (tmp_path / "my-tool" / "README.md").read_text()
        assert "command-line" in readme.lower() or "CLI" in readme

    def test_cli_config_no_module_field(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-tool",
                "--archetype",
                "cli",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        config = (tmp_path / "my-tool" / "pyfly.yaml").read_text()
        assert "module:" not in config

    def test_cli_config_no_web_section(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-tool",
                "--archetype",
                "cli",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        config = (tmp_path / "my-tool" / "pyfly.yaml").read_text()
        assert "web:" not in config
        assert "port: 8080" not in config

    def test_cli_pyproject_description(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-tool",
                "--archetype",
                "cli",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        pyproject = (tmp_path / "my-tool" / "pyproject.toml").read_text()
        assert "PyFly CLI application" in pyproject

    def test_cli_next_steps_show_python_m(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "new",
                "my-tool",
                "--archetype",
                "cli",
                "--directory",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "python -m my_tool.main" in result.output
        assert "pyfly run" not in result.output
