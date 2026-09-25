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
"""Multiple named datasources (v26.06.48).

C046: named datasource URLs go through the same typed reader as the primary, so ``${...}``
placeholders resolve and ``PYFLY_*`` overrides win; the auto-configured bean is a view over the
datasource registry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pyfly.core.config import Config
from pyfly.data.relational.named_datasources import NamedDataSources, build_named_data_sources


def test_holder_get_names_contains_len() -> None:
    nds = NamedDataSources({"reporting": "F_r", "audit": "F_a"})
    assert nds.get("reporting") == "F_r"
    assert nds.names() == ["audit", "reporting"]
    assert "audit" in nds
    assert len(nds) == 2
    with pytest.raises(KeyError, match="No datasource named"):
        nds.get("missing")


def test_build_from_config_skips_entries_without_url() -> None:
    cfg = Config(
        {
            "pyfly": {
                "data": {
                    "relational": {
                        "datasources": {
                            "reporting": {"url": "sqlite+aiosqlite:///r.db"},
                            "audit": {"url": "sqlite+aiosqlite:///a.db", "echo": True},
                            "broken": {"echo": True},  # no url -> skipped
                        }
                    }
                }
            }
        }
    )
    created: list[tuple[str, bool]] = []

    def engine_factory(url: str, echo: bool = False) -> str:
        created.append((url, echo))
        return f"engine:{url}"

    def session_factory(engine: str) -> str:
        return f"sm:{engine}"

    nds = build_named_data_sources(cfg, engine_factory, session_factory)
    assert nds.names() == ["audit", "reporting"]  # "broken" skipped (no url)
    assert nds.get("reporting") == "sm:engine:sqlite+aiosqlite:///r.db"
    assert ("sqlite+aiosqlite:///a.db", True) in created


def test_build_empty_when_no_datasources_configured() -> None:
    nds = build_named_data_sources(Config({}), lambda url, echo=False: None, lambda engine: None)
    assert nds.names() == []
    assert len(nds) == 0


@pytest.mark.asyncio
async def test_dispose_disposes_every_engine() -> None:
    disposed: list[str] = []

    class _FakeEngine:
        def __init__(self, name: str) -> None:
            self._name = name

        async def dispose(self) -> None:
            disposed.append(self._name)

    nds = NamedDataSources({"r": "sm", "a": "sm"}, {"r": _FakeEngine("r"), "a": _FakeEngine("a")})
    await nds.dispose()
    assert sorted(disposed) == ["a", "r"]


def test_build_resolves_placeholders_and_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("REPORTING_DIR", str(tmp_path))
    monkeypatch.setenv("PYFLY_DATA_RELATIONAL_DATASOURCES_ANALYTICS_URL", f"sqlite+aiosqlite:///{tmp_path}/a.db")
    cfg = Config(
        {
            "pyfly": {
                "data": {
                    "relational": {
                        "datasources": {
                            "reporting": {"url": "sqlite+aiosqlite:///${REPORTING_DIR}/r.db", "echo": "false"},
                            "analytics": {"url": "sqlite+aiosqlite:///./i-should-be-overridden.db"},
                        }
                    }
                }
            }
        }
    )
    created: dict[str, tuple[str, object]] = {}

    def engine_factory(url: str, echo: object = False) -> str:
        created[url] = (url, echo)
        return url

    nds = build_named_data_sources(cfg, engine_factory, lambda engine: f"sm:{engine}")
    assert nds.get("reporting") == f"sm:sqlite+aiosqlite:///{tmp_path}/r.db"
    assert nds.get("analytics") == f"sm:sqlite+aiosqlite:///{tmp_path}/a.db"
    assert created[f"sqlite+aiosqlite:///{tmp_path}/r.db"][1] is False  # "false" is False, not bool("false")


@pytest.mark.asyncio
async def test_registry_view_lists_named_and_module_datasources(tmp_path: Path) -> None:
    pytest.importorskip("sqlalchemy")
    from pyfly.data.relational.datasource_registry import DataSourceRegistry

    registry = DataSourceRegistry.for_config(
        Config(
            {
                "pyfly": {
                    "data": {
                        "relational": {
                            "url": f"sqlite+aiosqlite:///{tmp_path / 'p.db'}",
                            "datasources": {"reporting": {"url": f"sqlite+aiosqlite:///{tmp_path / 'r.db'}"}},
                        }
                    }
                }
            }
        )
    )
    try:
        nds = NamedDataSources.of_registry(registry)
        assert nds.names() == ["reporting"]
        assert nds.get("reporting") is registry.get("reporting").sessionmaker
        registry.resolve(f"sqlite+aiosqlite:///{tmp_path / 'e.db'}", name="event-store")
        assert nds.names() == ["event-store", "reporting"]  # live: a module's datasource appears
        assert "event-store" in nds and len(nds) == 2
        with pytest.raises(KeyError, match="primary"):
            nds.get("primary")  # the primary keeps its own beans
    finally:
        await registry.close()
