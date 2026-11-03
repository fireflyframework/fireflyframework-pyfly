# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
import math

import pytest
from pydantic import ValidationError

from pyfly.agentic.pipeline.durable import DurableBinding, DurableRunKey, DurableStateError
from pyfly.agentic.pipeline.durable.models import bounded_json
from pyfly.audit.models import AuditScope


def binding():
    return DurableBinding(
        pipeline_name="flow",
        definition_id="definition",
        definition_revision=1,
        graph_version="v1",
        graph_digest="a" * 64,
        plan_digest="b" * 64,
        node_ids=("a", "wait", "b"),
        pause_node_id="wait",
    )


def test_binding_is_closed_and_requires_unique_interior_pause():
    value = binding()
    assert value.pause_node_id == "wait"
    for update in (
        {"node_ids": ("a", "a", "b")},
        {"pause_node_id": "a"},
        {"definition_revision": True},
        {"unknown": "raw-secret"},
    ):
        with pytest.raises(ValidationError) as error:
            DurableBinding.model_validate(value.model_dump() | update)
        assert "raw-secret" not in str(error.value)


def test_scope_participates_in_exact_key_and_is_immutable():
    first = DurableRunKey(scope=AuditScope(tenant_id="one", workspace_id="w", project_id="p"), run_id="same")
    second = first.model_copy(update={"scope": AuditScope(tenant_id="two", workspace_id="w", project_id="p")})
    assert first != second
    with pytest.raises(ValidationError):
        first.run_id = "changed"


def test_serialized_envelope_rejects_non_json_nonfinite_depth_and_size_without_content():
    for value in (math.nan, {"value": object()}, {1: "number-key"}, "private-canary" * 100):
        with pytest.raises(DurableStateError) as error:
            bounded_json(value, max_bytes=32)
        assert "private-canary" not in str(error.value)
    nested = {}
    for _ in range(40):
        nested = {"next": nested}
    with pytest.raises(DurableStateError):
        bounded_json(nested)
    assert bounded_json({"nested": [None, True, 1, "text"]}) == '{"nested":[null,true,1,"text"]}'
