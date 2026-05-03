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
"""Tests for enhanced BeansProvider -- metrics, detail, and graph."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from pyfly.admin.providers.beans_provider import BeansProvider
from pyfly.container.autowired import Autowired
from pyfly.container.metrics import BeanMetrics
from pyfly.container.types import Scope

# ---------------------------------------------------------------------------
# Sample classes used across tests
# ---------------------------------------------------------------------------


class Repository:
    """A sample repository bean."""


class Service:
    """A sample service bean with a dependency on Repository."""

    __pyfly_stereotype__ = "service"

    def __init__(self, repo: Repository) -> None:
        self.repo = repo


class Controller:
    """A sample controller depending on Service."""

    __pyfly_stereotype__ = "rest_controller"

    def __init__(self, service: Service) -> None:
        self.service = service


class StandaloneBean:
    """A bean with no dependencies."""


class LifecycleBean:
    """A bean with lifecycle hooks."""

    def on_start(self) -> None:  # pragma: no cover
        ...

    on_start.__pyfly_post_construct__ = True  # type: ignore[attr-defined]

    def on_stop(self) -> None:  # pragma: no cover
        ...

    on_stop.__pyfly_pre_destroy__ = True  # type: ignore[attr-defined]


class AutowiredBean:
    """A bean with Autowired field-injection descriptors."""

    repo: Repository = Autowired()
    cache: object = Autowired(qualifier="redis", required=False)


class ConditionalBean:
    """A bean with conditions."""

    __pyfly_conditions__ = [
        {"type": "on_property", "key": "feature.x", "having_value": "true"},
        {"type": "on_class", "module_name": "json", "check": lambda: True},
    ]


class FailingConditionBean:
    """A bean whose on_class condition fails."""

    __pyfly_conditions__ = [
        {"type": "on_class", "module_name": "nonexistent", "check": lambda: False},
    ]


class BeanMethodBean:
    """A bean created via @bean method."""

    __pyfly_bean_method__ = "AppConfig.create_client"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_reg(
    cls: type,
    name: str = "",
    scope: Scope = Scope.SINGLETON,
    instance: object | None = None,
) -> MagicMock:
    reg = MagicMock()
    reg.name = name
    reg.scope = scope
    reg.impl_type = cls
    reg.instance = instance
    return reg


def _make_context(
    registrations: dict[type, MagicMock],
    metrics: dict[type, BeanMetrics] | None = None,
) -> MagicMock:
    """Build a mock ApplicationContext with a mock container."""
    ctx = MagicMock()
    ctx.container._registrations = registrations
    metrics = metrics or {}

    def _get_bean_metrics(cls: type) -> BeanMetrics | None:
        return metrics.get(cls)

    def _get_all_metrics() -> dict[type, BeanMetrics]:
        return dict(metrics)

    ctx.container.get_bean_metrics = MagicMock(side_effect=_get_bean_metrics)
    ctx.container.get_all_metrics = MagicMock(side_effect=_get_all_metrics)
    return ctx


# ===================================================================
# Tests for get_beans()
# ===================================================================


class TestGetBeansMetrics:
    """get_beans() must include creation_time_ms, resolution_count, created_at."""

    async def test_beans_include_metrics_fields_with_data(self):
        now = time.time()
        regs = {
            Service: _make_reg(Service, name="myService", instance=Service.__new__(Service)),
        }
        metrics = {
            Service: BeanMetrics(creation_time_ns=2_500_000, resolution_count=7, created_at=now),
        }
        provider = BeansProvider(_make_context(regs, metrics))
        result = await provider.get_beans()

        bean = result["beans"][0]
        assert bean["creation_time_ms"] == 2.5
        assert bean["resolution_count"] == 7
        assert bean["created_at"] == now

    async def test_beans_metrics_none_when_never_resolved(self):
        regs = {StandaloneBean: _make_reg(StandaloneBean, name="standalone")}
        provider = BeansProvider(_make_context(regs, {}))
        result = await provider.get_beans()

        bean = result["beans"][0]
        assert bean["creation_time_ms"] is None
        assert bean["resolution_count"] == 0
        assert bean["created_at"] is None

    async def test_beans_metrics_rounding(self):
        regs = {StandaloneBean: _make_reg(StandaloneBean, name="standalone")}
        metrics = {
            StandaloneBean: BeanMetrics(creation_time_ns=1_234_567, resolution_count=1, created_at=1.0),
        }
        provider = BeansProvider(_make_context(regs, metrics))
        result = await provider.get_beans()

        bean = result["beans"][0]
        assert bean["creation_time_ms"] == 1.23  # 1_234_567 / 1_000_000 rounded to 2

    async def test_beans_total_and_sorting(self):
        regs = {
            Service: _make_reg(Service, name="myService"),
            Repository: _make_reg(Repository, name="myRepo"),
        }
        provider = BeansProvider(_make_context(regs))
        result = await provider.get_beans()

        assert result["total"] == 2
        # Sorted by (stereotype, name): "none"/"myRepo" before "service"/"myService"
        assert result["beans"][0]["name"] == "myRepo"
        assert result["beans"][1]["name"] == "myService"


# ===================================================================
# Tests for get_bean_detail()
# ===================================================================


class TestGetBeanDetail:
    """get_bean_detail() must include enriched information."""

    async def test_detail_dependency_chain(self):
        regs = {
            Controller: _make_reg(Controller, name="ctrl"),
            Service: _make_reg(Service, name="svc"),
            Repository: _make_reg(Repository, name="repo"),
        }
        provider = BeansProvider(_make_context(regs))
        detail = await provider.get_bean_detail("ctrl")

        assert detail is not None
        chain = detail["dependency_chain"]
        assert len(chain) == 1
        assert chain[0]["name"] == "service"
        assert chain[0]["type"] == "Service"
        # Service -> Repository (recursive)
        assert len(chain[0]["dependencies"]) == 1
        assert chain[0]["dependencies"][0]["name"] == "repo"
        assert chain[0]["dependencies"][0]["type"] == "Repository"

    async def test_detail_conditions_with_pass_status(self):
        regs = {ConditionalBean: _make_reg(ConditionalBean, name="condBean")}
        provider = BeansProvider(_make_context(regs))
        detail = await provider.get_bean_detail("condBean")

        assert detail is not None
        conditions = detail["conditions"]
        assert len(conditions) == 2
        # on_property -- assumed passed (bean is registered)
        assert conditions[0]["type"] == "on_property"
        assert conditions[0]["passed"] is True
        # on_class -- evaluated via check()
        assert conditions[1]["type"] == "on_class"
        assert conditions[1]["passed"] is True
        # check callable must NOT appear in serialised output
        assert "check" not in conditions[0]
        assert "check" not in conditions[1]

    async def test_detail_conditions_failing(self):
        regs = {FailingConditionBean: _make_reg(FailingConditionBean, name="failBean")}
        provider = BeansProvider(_make_context(regs))
        detail = await provider.get_bean_detail("failBean")

        assert detail is not None
        conditions = detail["conditions"]
        assert len(conditions) == 1
        assert conditions[0]["passed"] is False

    async def test_detail_lifecycle_methods(self):
        regs = {LifecycleBean: _make_reg(LifecycleBean, name="lcBean")}
        provider = BeansProvider(_make_context(regs))
        detail = await provider.get_bean_detail("lcBean")

        assert detail is not None
        assert "on_start" in detail["post_construct"]
        assert "on_stop" in detail["pre_destroy"]

    async def test_detail_autowired_fields(self):
        regs = {AutowiredBean: _make_reg(AutowiredBean, name="awBean")}
        provider = BeansProvider(_make_context(regs))
        detail = await provider.get_bean_detail("awBean")

        assert detail is not None
        aw_fields = detail["autowired_fields"]
        names = {f["name"] for f in aw_fields}
        assert "repo" in names
        assert "cache" in names

        cache_field = next(f for f in aw_fields if f["name"] == "cache")
        assert cache_field["qualifier"] == "redis"
        assert cache_field["required"] is False

    async def test_detail_bean_method_origin(self):
        regs = {BeanMethodBean: _make_reg(BeanMethodBean, name="bmBean")}
        provider = BeansProvider(_make_context(regs))
        detail = await provider.get_bean_detail("bmBean")

        assert detail is not None
        assert detail["bean_method_origin"] == "AppConfig.create_client"

    async def test_detail_bean_method_origin_none_when_absent(self):
        regs = {StandaloneBean: _make_reg(StandaloneBean, name="standalone")}
        provider = BeansProvider(_make_context(regs))
        detail = await provider.get_bean_detail("standalone")

        assert detail is not None
        assert detail["bean_method_origin"] is None

    async def test_detail_metrics_included(self):
        now = time.time()
        regs = {Service: _make_reg(Service, name="svc", instance=Service.__new__(Service))}
        metrics = {Service: BeanMetrics(creation_time_ns=3_000_000, resolution_count=5, created_at=now)}
        provider = BeansProvider(_make_context(regs, metrics))
        detail = await provider.get_bean_detail("svc")

        assert detail is not None
        assert detail["creation_time_ms"] == 3.0
        assert detail["resolution_count"] == 5
        assert detail["created_at"] == now

    async def test_detail_metrics_defaults_when_no_metrics(self):
        regs = {StandaloneBean: _make_reg(StandaloneBean, name="standalone")}
        provider = BeansProvider(_make_context(regs, {}))
        detail = await provider.get_bean_detail("standalone")

        assert detail is not None
        assert detail["creation_time_ms"] is None
        assert detail["resolution_count"] == 0
        assert detail["created_at"] is None

    async def test_detail_not_found(self):
        provider = BeansProvider(_make_context({}))
        assert await provider.get_bean_detail("does_not_exist") is None

    async def test_detail_standard_fields_present(self):
        regs = {Service: _make_reg(Service, name="svc")}
        provider = BeansProvider(_make_context(regs))
        detail = await provider.get_bean_detail("svc")

        assert detail is not None
        assert detail["name"] == "svc"
        assert "Service" in detail["type"]
        assert detail["scope"] == "SINGLETON"
        assert detail["stereotype"] == "service"
        assert detail["doc"] != ""


# ===================================================================
# Tests for get_bean_graph()
# ===================================================================


class TestGetBeanGraph:
    """get_bean_graph() must return {nodes, edges}."""

    async def test_graph_structure(self):
        regs = {
            Repository: _make_reg(Repository, name="repo"),
            Service: _make_reg(Service, name="svc"),
            Controller: _make_reg(Controller, name="ctrl"),
        }
        metrics = {
            Service: BeanMetrics(resolution_count=3),
            Repository: BeanMetrics(resolution_count=5),
        }
        provider = BeansProvider(_make_context(regs, metrics))
        graph = await provider.get_bean_graph()

        assert "nodes" in graph
        assert "edges" in graph
        assert len(graph["nodes"]) == 3
        node_ids = {n["id"] for n in graph["nodes"]}
        assert node_ids == {"repo", "svc", "ctrl"}

    async def test_graph_edges(self):
        regs = {
            Repository: _make_reg(Repository, name="repo"),
            Service: _make_reg(Service, name="svc"),
            Controller: _make_reg(Controller, name="ctrl"),
        }
        provider = BeansProvider(_make_context(regs))
        graph = await provider.get_bean_graph()

        edges = graph["edges"]
        edge_tuples = {(e["source"], e["target"]) for e in edges}
        assert ("ctrl", "svc") in edge_tuples
        assert ("svc", "repo") in edge_tuples

    async def test_graph_node_contains_expected_fields(self):
        regs = {Service: _make_reg(Service, name="svc")}
        metrics = {Service: BeanMetrics(resolution_count=2)}
        provider = BeansProvider(_make_context(regs, metrics))
        graph = await provider.get_bean_graph()

        node = graph["nodes"][0]
        assert node["id"] == "svc"
        assert node["name"] == "svc"
        assert "Service" in node["type"]
        assert node["stereotype"] == "service"
        assert node["resolution_count"] == 2

    async def test_graph_empty(self):
        provider = BeansProvider(_make_context({}))
        graph = await provider.get_bean_graph()

        assert graph["nodes"] == []
        assert graph["edges"] == []

    async def test_graph_no_edges_for_standalone_beans(self):
        regs = {
            StandaloneBean: _make_reg(StandaloneBean, name="standalone"),
            Repository: _make_reg(Repository, name="repo"),
        }
        provider = BeansProvider(_make_context(regs))
        graph = await provider.get_bean_graph()

        assert len(graph["nodes"]) == 2
        assert len(graph["edges"]) == 0

    async def test_graph_resolution_count_zero_without_metrics(self):
        regs = {StandaloneBean: _make_reg(StandaloneBean, name="standalone")}
        provider = BeansProvider(_make_context(regs, {}))
        graph = await provider.get_bean_graph()

        assert graph["nodes"][0]["resolution_count"] == 0
