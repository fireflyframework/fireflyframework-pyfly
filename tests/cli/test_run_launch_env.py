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
