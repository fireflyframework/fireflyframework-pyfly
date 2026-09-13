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
"""Tests for 'pyfly run' launch-env construction (profiles, -D, --env, --debug)."""

from __future__ import annotations

import pytest

from pyfly.cli.run import _build_launch_env, _to_env_key


class TestToEnvKey:
    def test_strips_pyfly_prefix_and_uppercases(self) -> None:
        assert _to_env_key("pyfly.web.port") == "PYFLY_WEB_PORT"

    def test_bare_key_gets_prefix(self) -> None:
        assert _to_env_key("web.port") == "PYFLY_WEB_PORT"

    def test_dashes_become_underscores(self) -> None:
        assert _to_env_key("server.graceful-timeout") == "PYFLY_SERVER_GRACEFUL_TIMEOUT"


class TestBuildLaunchEnv:
    def test_profiles_joined(self) -> None:
        env = _build_launch_env(("prod", "cloud"), (), (), debug=False)
        assert env["PYFLY_PROFILES_ACTIVE"] == "prod,cloud"

    def test_profiles_flatten_commas(self) -> None:
        env = _build_launch_env(("prod,cloud",), (), (), debug=False)
        assert env["PYFLY_PROFILES_ACTIVE"] == "prod,cloud"

    def test_define_mapped_to_env_key(self) -> None:
        env = _build_launch_env((), ("web.port=9000",), (), debug=False)
        assert env["PYFLY_WEB_PORT"] == "9000"

    def test_raw_env_passthrough(self) -> None:
        env = _build_launch_env((), (), ("FOO=bar",), debug=False)
        assert env["FOO"] == "bar"

    def test_debug_sets_root_log_level(self) -> None:
        env = _build_launch_env((), (), (), debug=True)
        assert env["PYFLY_LOGGING_LEVEL_ROOT"] == "DEBUG"

    def test_empty_when_nothing(self) -> None:
        assert _build_launch_env((), (), (), debug=False) == {}

    def test_bad_define_raises(self) -> None:
        import click

        with pytest.raises(click.BadParameter):
            _build_launch_env((), ("noequals",), (), debug=False)


class TestReadPortFromConfig:
    """``pyfly run`` must resolve the port the way the application does.

    The raw ``pyfly.yaml`` read ignored the relaxed-binding override
    ``PYFLY_SERVER_PORT`` (which is also what ``-D server.port=…`` becomes) and the
    profile overlays, so the CLI bound the base port while the app believed another.
    """

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.delenv("PYFLY_SERVER_PORT", raising=False)
        monkeypatch.delenv("PYFLY_PROFILES_ACTIVE", raising=False)
        monkeypatch.chdir(tmp_path)

    def test_no_config_file_means_no_port(self) -> None:
        from pyfly.cli.run import _read_port_from_config

        assert _read_port_from_config() is None

    def test_reads_server_port_from_pyfly_yaml(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from pyfly.cli.run import _read_port_from_config

        (tmp_path / "pyfly.yaml").write_text("pyfly:\n  server:\n    port: 8085\n")
        assert _read_port_from_config() == 8085

    def test_env_override_wins_over_yaml(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
        from pyfly.cli.run import _read_port_from_config

        (tmp_path / "pyfly.yaml").write_text("pyfly:\n  server:\n    port: 8085\n")
        monkeypatch.setenv("PYFLY_SERVER_PORT", "8090")
        assert _read_port_from_config() == 8090

    def test_env_override_works_without_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pyfly.cli.run import _read_port_from_config

        monkeypatch.setenv("PYFLY_SERVER_PORT", "8090")
        assert _read_port_from_config() == 8090

    def test_define_flag_reaches_the_port(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
        """``-D server.port=9000`` is what the CLI help advertises; it must bind 9000."""
        import os

        from pyfly.cli.run import _read_port_from_config

        (tmp_path / "pyfly.yaml").write_text("pyfly:\n  server:\n    port: 8085\n")
        for key, value in _build_launch_env((), ("server.port=9000",), (), debug=False).items():
            monkeypatch.setenv(key, value)
        assert os.environ["PYFLY_SERVER_PORT"] == "9000"
        assert _read_port_from_config() == 9000

    def test_profile_overlay_wins_over_base(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
        from pyfly.cli.run import _read_port_from_config

        (tmp_path / "pyfly.yaml").write_text("pyfly:\n  server:\n    port: 8085\n")
        (tmp_path / "pyfly-dev.yaml").write_text("pyfly:\n  server:\n    port: 8095\n")
        monkeypatch.setenv("PYFLY_PROFILES_ACTIVE", "dev")
        assert _read_port_from_config() == 8095

    def test_bad_env_value_is_a_usage_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import click

        from pyfly.cli.run import _read_port_from_config

        monkeypatch.setenv("PYFLY_SERVER_PORT", "eighty")
        with pytest.raises(click.BadParameter):
            _read_port_from_config()
