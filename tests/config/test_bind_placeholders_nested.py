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
"""Regression: Config.bind() resolves ${...} placeholders and binds nested dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field

from pyfly.core.config import Config, config_properties


@dataclass
class _Granian:
    workers: int = 1
    threads: int = 2


@config_properties("server")
@dataclass
class _Server:
    type: str = "auto"
    granian: _Granian = field(default_factory=_Granian)


@config_properties("svc")
@dataclass
class _Svc:
    url: str = ""
    port: int = 0


def test_bind_resolves_placeholders(monkeypatch):
    monkeypatch.setenv("MY_PORT", "9000")
    cfg = Config({"svc": {"url": "http://host:${MY_PORT}/x", "port": "${MY_PORT}"}})
    svc = cfg.bind(_Svc)
    assert svc.url == "http://host:9000/x"
    assert svc.port == 9000  # placeholder resolved, then coerced to int


def test_bind_uses_placeholder_default():
    cfg = Config({"svc": {"url": "http://host:${MISSING_PORT:8080}"}})
    svc = cfg.bind(_Svc)
    assert svc.url == "http://host:8080"


def test_bind_binds_nested_dataclass():
    cfg = Config({"server": {"type": "uvicorn", "granian": {"workers": 4, "threads": 8}}})
    server = cfg.bind(_Server)
    assert server.type == "uvicorn"
    assert isinstance(server.granian, _Granian)
    assert server.granian.workers == 4
    assert server.granian.threads == 8


def test_bind_nested_dataclass_default_when_absent():
    cfg = Config({"server": {"type": "uvicorn"}})
    server = cfg.bind(_Server)
    assert isinstance(server.granian, _Granian)
    assert server.granian.workers == 1
