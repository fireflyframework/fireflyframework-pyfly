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
"""Tests for ExecutionContext and CorrelationContext."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from pyfly.cqrs.context.execution_context import (
    DefaultExecutionContext,
    ExecutionContext,
    ExecutionContextBuilder,
)
from pyfly.cqrs.tracing.correlation import (
    HEADER_CORRELATION_ID,
    HEADER_SPAN_ID,
    HEADER_TRACE_ID,
    CorrelationContext,
)


class TestDefaultExecutionContext:
    def test_frozen_immutability(self) -> None:
        ctx = DefaultExecutionContext(user_id="u1")
        with pytest.raises(FrozenInstanceError):
            ctx.user_id = "u2"  # type: ignore[misc]

    def test_default_values(self) -> None:
        ctx = DefaultExecutionContext()
        assert ctx.user_id is None
        assert ctx.tenant_id is None
        assert ctx.organization_id is None
        assert ctx.session_id is None
        assert ctx.request_id is None
        assert ctx.source is None
        assert ctx.client_ip is None
        assert ctx.user_agent is None
        assert isinstance(ctx.created_at, datetime)
        assert ctx.properties == {}
        assert ctx.feature_flags == {}

    def test_get_feature_flag_existing(self) -> None:
        ctx = DefaultExecutionContext(feature_flags={"dark_mode": True, "beta": False})
        assert ctx.get_feature_flag("dark_mode") is True
        assert ctx.get_feature_flag("beta") is False

    def test_get_feature_flag_missing_returns_default(self) -> None:
        ctx = DefaultExecutionContext()
        assert ctx.get_feature_flag("unknown") is False
        assert ctx.get_feature_flag("unknown", default=True) is True

    def test_get_property_existing(self) -> None:
        ctx = DefaultExecutionContext(properties={"region": "us-east-1"})
        assert ctx.get_property("region") == "us-east-1"

    def test_get_property_missing_returns_none(self) -> None:
        ctx = DefaultExecutionContext()
        assert ctx.get_property("nonexistent") is None

    def test_satisfies_execution_context_protocol(self) -> None:
        ctx = DefaultExecutionContext(user_id="u1")
        assert isinstance(ctx, ExecutionContext)

    def test_created_at_is_utc(self) -> None:
        ctx = DefaultExecutionContext()
        assert ctx.created_at.tzinfo is not None
        assert ctx.created_at.tzinfo == UTC


class TestExecutionContextBuilder:
    def test_builder_sets_all_fields(self) -> None:
        ctx = (
            ExecutionContextBuilder()
            .with_user_id("user-42")
            .with_tenant_id("tenant-1")
            .with_organization_id("org-99")
            .with_session_id("sess-abc")
            .with_request_id("req-xyz")
            .with_source("web")
            .with_client_ip("192.168.1.1")
            .with_user_agent("Mozilla/5.0")
            .with_property("locale", "en_US")
            .with_feature_flag("dark_mode", True)
            .build()
        )

        assert ctx.user_id == "user-42"
        assert ctx.tenant_id == "tenant-1"
        assert ctx.organization_id == "org-99"
        assert ctx.session_id == "sess-abc"
        assert ctx.request_id == "req-xyz"
        assert ctx.source == "web"
        assert ctx.client_ip == "192.168.1.1"
        assert ctx.user_agent == "Mozilla/5.0"
        assert ctx.get_property("locale") == "en_US"
        assert ctx.get_feature_flag("dark_mode") is True

    def test_builder_produces_frozen_instance(self) -> None:
        ctx = ExecutionContextBuilder().with_user_id("u1").build()
        with pytest.raises(FrozenInstanceError):
            ctx.user_id = "u2"  # type: ignore[misc]

    def test_builder_default_created_at(self) -> None:
        before = datetime.now(UTC)
        ctx = ExecutionContextBuilder().build()
        after = datetime.now(UTC)
        assert before <= ctx.created_at <= after

    def test_builder_multiple_properties(self) -> None:
        ctx = ExecutionContextBuilder().with_property("a", 1).with_property("b", "two").build()
        assert ctx.get_property("a") == 1
        assert ctx.get_property("b") == "two"

    def test_builder_multiple_feature_flags(self) -> None:
        ctx = ExecutionContextBuilder().with_feature_flag("flag_a", True).with_feature_flag("flag_b", False).build()
        assert ctx.get_feature_flag("flag_a") is True
        assert ctx.get_feature_flag("flag_b") is False

    def test_builder_returns_self_for_chaining(self) -> None:
        builder = ExecutionContextBuilder()
        result = builder.with_user_id("u1")
        assert result is builder


class TestCorrelationContext:
    @pytest.fixture(autouse=True)
    def _clear_context(self) -> None:
        CorrelationContext.clear()

    def test_get_or_create_generates_new_id(self) -> None:
        assert CorrelationContext.get_correlation_id() is None
        cid = CorrelationContext.get_or_create_correlation_id()
        assert cid is not None
        assert len(cid) > 0

    def test_get_or_create_returns_existing(self) -> None:
        CorrelationContext.set_correlation_id("existing-id")
        cid = CorrelationContext.get_or_create_correlation_id()
        assert cid == "existing-id"

    def test_set_and_get_correlation_id(self) -> None:
        CorrelationContext.set_correlation_id("corr-123")
        assert CorrelationContext.get_correlation_id() == "corr-123"

    def test_set_and_get_trace_id(self) -> None:
        CorrelationContext.set_trace_id("trace-abc")
        assert CorrelationContext.get_trace_id() == "trace-abc"

    def test_set_and_get_span_id(self) -> None:
        CorrelationContext.set_span_id("span-xyz")
        assert CorrelationContext.get_span_id() == "span-xyz"

    def test_create_context_headers_includes_set_ids(self) -> None:
        CorrelationContext.set_correlation_id("corr-1")
        CorrelationContext.set_trace_id("trace-2")
        CorrelationContext.set_span_id("span-3")

        headers = CorrelationContext.create_context_headers()
        assert headers[HEADER_CORRELATION_ID] == "corr-1"
        assert headers[HEADER_TRACE_ID] == "trace-2"
        assert headers[HEADER_SPAN_ID] == "span-3"

    def test_create_context_headers_excludes_unset_ids(self) -> None:
        CorrelationContext.set_correlation_id("corr-1")
        headers = CorrelationContext.create_context_headers()
        assert HEADER_CORRELATION_ID in headers
        assert HEADER_TRACE_ID not in headers
        assert HEADER_SPAN_ID not in headers

    def test_create_context_headers_empty_when_nothing_set(self) -> None:
        headers = CorrelationContext.create_context_headers()
        assert headers == {}

    def test_extract_context_from_headers_restores_all(self) -> None:
        headers = {
            HEADER_CORRELATION_ID: "restored-corr",
            HEADER_TRACE_ID: "restored-trace",
            HEADER_SPAN_ID: "restored-span",
        }
        CorrelationContext.extract_context_from_headers(headers)

        assert CorrelationContext.get_correlation_id() == "restored-corr"
        assert CorrelationContext.get_trace_id() == "restored-trace"
        assert CorrelationContext.get_span_id() == "restored-span"

    def test_extract_context_from_headers_partial(self) -> None:
        headers = {HEADER_CORRELATION_ID: "only-corr"}
        CorrelationContext.extract_context_from_headers(headers)
        assert CorrelationContext.get_correlation_id() == "only-corr"
        assert CorrelationContext.get_trace_id() is None

    def test_clear_resets_all(self) -> None:
        CorrelationContext.set_correlation_id("c1")
        CorrelationContext.set_trace_id("t1")
        CorrelationContext.set_span_id("s1")

        CorrelationContext.clear()

        assert CorrelationContext.get_correlation_id() is None
        assert CorrelationContext.get_trace_id() is None
        assert CorrelationContext.get_span_id() is None

    def test_generate_correlation_id_returns_uuid_string(self) -> None:
        cid = CorrelationContext.generate_correlation_id()
        assert isinstance(cid, str)
        assert len(cid) == 36  # UUID format: 8-4-4-4-12

    def test_roundtrip_headers(self) -> None:
        CorrelationContext.set_correlation_id("rt-corr")
        CorrelationContext.set_trace_id("rt-trace")
        CorrelationContext.set_span_id("rt-span")

        headers = CorrelationContext.create_context_headers()
        CorrelationContext.clear()

        assert CorrelationContext.get_correlation_id() is None

        CorrelationContext.extract_context_from_headers(headers)
        assert CorrelationContext.get_correlation_id() == "rt-corr"
        assert CorrelationContext.get_trace_id() == "rt-trace"
        assert CorrelationContext.get_span_id() == "rt-span"
