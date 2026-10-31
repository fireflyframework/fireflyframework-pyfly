# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Host-owned identity facts cannot be expanded into arbitrary content metadata."""

import importlib

import pytest
from pydantic import ValidationError

from pyfly.audit import AuditActor, AuditResource, AuditScope


def api():
    module = importlib.import_module("pyfly.audit")
    assert hasattr(module, "AuditContext") and hasattr(module, "AuditOperation")
    return module


def test_trusted_context_accepts_exact_references_but_no_request_content_or_identity_overrides():
    contract = api()
    context = contract.AuditContext(
        scope=AuditScope(tenant_id="tenant", workspace_id="workspace", project_id="project"),
        actor=AuditActor(kind="service", subject_id="verified-subject"),
        resource=AuditResource(kind="document", resource_id="opaque-document", revision=3),
    )
    assert context.resource.revision == 3
    with pytest.raises(ValidationError):
        contract.AuditContext(**context.model_dump(), body="private payload")
    with pytest.raises(ValidationError):
        contract.AuditOperation(module="web", action="request", actor="forged")
    with pytest.raises(ValidationError):
        context.actor = AuditActor(kind="service", subject_id="forged")


def test_operation_resource_key_is_only_a_bounded_resolver_hint():
    contract = api()
    operation = contract.AuditOperation(module="eda", action="enqueue", resource_kind="delivery", resource_key="42")
    assert operation.resource_key == "42"
    for value in ("https://private.example/api", "raw request body", "Bearer fixture-token"):
        with pytest.raises(ValidationError) as caught:
            contract.AuditOperation(module="web", action="request", resource_key=value)
        assert value not in str(caught.value)
