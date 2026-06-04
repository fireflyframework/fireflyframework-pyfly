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
"""Regression tests for #84 — ConfigClient is invoked at bootstrap.

These are intentionally synchronous tests: PyFlyApplication.__init__ uses
asyncio.run() for the remote fetch, which requires no event loop be running.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from pyfly.config_server.client import ConfigClient
from pyfly.core.application import PyFlyApplication, pyfly_application


@pyfly_application(name="svc", scan_packages=[])
class _App: ...


def _write_yaml(tmp_path: Path, body: str) -> str:
    path = tmp_path / "pyfly.yaml"
    path.write_text(body)
    return str(path)


def test_remote_config_merged_at_bootstrap(tmp_path, monkeypatch):
    monkeypatch.setattr(ConfigClient, "fetch", AsyncMock(return_value={"pyfly.remote.key": "fromserver"}))
    cfg = _write_yaml(tmp_path, "pyfly:\n  cloud:\n    config:\n      uri: http://cfg\n  local:\n    key: localvalue\n")

    app = PyFlyApplication(_App, config_path=cfg)

    assert app.config.get("pyfly.remote.key") == "fromserver"
    assert app.config.get("pyfly.local.key") == "localvalue"  # local config preserved
    assert any("config-server" in s for s in app.config.loaded_sources)


def test_no_uri_skips_remote_fetch(tmp_path, monkeypatch):
    fetch = AsyncMock(return_value={"pyfly.remote.key": "x"})
    monkeypatch.setattr(ConfigClient, "fetch", fetch)
    cfg = _write_yaml(tmp_path, "pyfly:\n  local:\n    key: localvalue\n")

    PyFlyApplication(_App, config_path=cfg)
    assert fetch.called is False


def test_remote_failure_is_non_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(ConfigClient, "fetch", AsyncMock(side_effect=RuntimeError("server down")))
    cfg = _write_yaml(tmp_path, "pyfly:\n  cloud:\n    config:\n      uri: http://cfg\n  local:\n    key: localvalue\n")

    app = PyFlyApplication(_App, config_path=cfg)  # must not raise
    assert app.config.get("pyfly.local.key") == "localvalue"


def test_fail_fast_propagates(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setattr(ConfigClient, "fetch", AsyncMock(side_effect=RuntimeError("server down")))
    cfg = _write_yaml(
        tmp_path,
        "pyfly:\n  cloud:\n    config:\n      uri: http://cfg\n      fail-fast: true\n",
    )

    with pytest.raises(RuntimeError, match="server down"):
        PyFlyApplication(_App, config_path=cfg)
