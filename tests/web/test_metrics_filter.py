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
"""Tests for MetricsFilter — Spring Boot / Micrometer-compatible HTTP metrics."""

from __future__ import annotations

import pytest
from prometheus_client import REGISTRY, generate_latest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from pyfly.web.adapters.starlette.filter_chain import WebFilterChainMiddleware
from pyfly.web.adapters.starlette.filters import metrics_filter as mf
from pyfly.web.adapters.starlette.filters.metrics_filter import MetricsFilter, _outcome


@pytest.fixture(autouse=True)
def _fresh_collectors():
    """Each test gets brand-new, isolated request-timer collectors."""
    mf.reset_collectors()
    yield
    mf.reset_collectors()


def _build_app(*, histogram: bool = False) -> Starlette:
    async def get_user(request):  # noqa: ANN001
        return JSONResponse({"id": request.path_params["user_id"]})

    async def boom(request):  # noqa: ANN001
        raise RuntimeError("kaboom")

    return Starlette(
        middleware=[Middleware(WebFilterChainMiddleware, filters=[MetricsFilter(histogram=histogram)])],
        routes=[
            Route("/users/{user_id}", get_user, methods=["GET"]),
            Route("/boom", boom, methods=["GET"]),
        ],
    )


def _timer_count(method: str, uri: str, status: str, outcome: str, exception: str) -> float:
    child = mf._timer.labels(method, uri, status, outcome, exception)
    return child._count.get()


class TestMicrometerNaming:
    def test_meter_is_named_http_server_requests_seconds(self) -> None:
        client = TestClient(_build_app())
        client.get("/users/42")

        exposition = generate_latest(REGISTRY).decode()
        # Spring Boot / Micrometer exact names — drives Grafana/Prometheus tooling.
        assert "http_server_requests_seconds_count" in exposition
        assert "http_server_requests_seconds_sum" in exposition
        assert "http_server_requests_seconds_max" in exposition
        # The legacy ad-hoc names must be gone.
        assert "http_requests_total" not in exposition
        assert "http_request_duration_seconds" not in exposition

    def test_uri_tag_is_templated_not_raw_path(self) -> None:
        """The cardinality-safe ``uri`` tag must be the route template, not /users/42."""
        client = TestClient(_build_app())
        client.get("/users/42")
        client.get("/users/99")

        # Both requests collapse onto the single templated series.
        assert _timer_count("GET", "/users/{user_id}", "200", "SUCCESS", "None") == 2.0
        # The raw path must never appear as a label value.
        exposition = generate_latest(REGISTRY).decode()
        assert 'uri="/users/42"' not in exposition
        assert 'uri="/users/{user_id}"' in exposition

    def test_success_outcome_and_status_tags(self) -> None:
        client = TestClient(_build_app())
        client.get("/users/7")
        assert _timer_count("GET", "/users/{user_id}", "200", "SUCCESS", "None") == 1.0

    def test_not_found_uri_and_client_error_outcome(self) -> None:
        client = TestClient(_build_app())
        client.get("/nope")
        # Unmatched route -> uri NOT_FOUND, outcome CLIENT_ERROR (Micrometer semantics).
        assert _timer_count("GET", "NOT_FOUND", "404", "CLIENT_ERROR", "None") == 1.0

    def test_server_error_records_exception_class(self) -> None:
        client = TestClient(_build_app(), raise_server_exceptions=False)
        client.get("/boom")
        # exception tag carries the thrown class; outcome SERVER_ERROR; status 500.
        assert _timer_count("GET", "/boom", "500", "SERVER_ERROR", "RuntimeError") == 1.0

    def test_max_gauge_is_populated(self) -> None:
        client = TestClient(_build_app())
        client.get("/users/1")
        child = mf._max_gauge.labels("GET", "/users/{user_id}", "200", "SUCCESS", "None")
        assert child._value.get() > 0.0

    def test_histogram_mode_emits_buckets(self) -> None:
        client = TestClient(_build_app(histogram=True))
        client.get("/users/1")
        exposition = generate_latest(REGISTRY).decode()
        assert "http_server_requests_seconds_bucket" in exposition


class TestOutcomeMapping:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (100, "INFORMATIONAL"),
            (200, "SUCCESS"),
            (204, "SUCCESS"),
            (301, "REDIRECTION"),
            (404, "CLIENT_ERROR"),
            (422, "CLIENT_ERROR"),
            (500, "SERVER_ERROR"),
            (503, "SERVER_ERROR"),
            (700, "UNKNOWN"),
        ],
    )
    def test_outcome_for_status(self, status: int, expected: str) -> None:
        assert _outcome(status) == expected


class TestExclusions:
    def test_excludes_prometheus_scrape_endpoint(self) -> None:
        f = MetricsFilter()

        class _Req:
            class url:  # noqa: N801
                path = "/actuator/prometheus"

        assert f.should_not_filter(_Req()) is True

    def test_excludes_admin_sse_streams(self) -> None:
        f = MetricsFilter()

        class _Req:
            class url:  # noqa: N801
                path = "/admin/api/sse/metrics"

        assert f.should_not_filter(_Req()) is True

    def test_does_not_exclude_normal_routes(self) -> None:
        f = MetricsFilter()

        class _Req:
            class url:  # noqa: N801
                path = "/api/users"

        assert f.should_not_filter(_Req()) is False
