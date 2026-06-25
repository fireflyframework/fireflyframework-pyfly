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
"""PermissionEvaluator SPI wired into the method-security expression engine."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from pyfly.security.context import SecurityContext
from pyfly.security.expression import evaluate_security_expression, set_permission_evaluator
from pyfly.security.permission import PermissionEvaluator


class _OwnerEvaluator:
    def has_permission(self, context: Any, target: Any, permission: str, *, target_type: str | None = None) -> bool:
        return target == "owned" and permission == "read"


@pytest.fixture(autouse=True)
def _reset_evaluator() -> Iterator[None]:
    yield
    set_permission_evaluator(None)


def test_protocol_conformance() -> None:
    assert isinstance(_OwnerEvaluator(), PermissionEvaluator)


def test_evaluator_receives_target_and_permission() -> None:
    set_permission_evaluator(_OwnerEvaluator())
    ctx = SecurityContext(user_id="u")
    assert evaluate_security_expression("hasPermission(#doc, 'read')", ctx, args={"doc": "owned"}) is True
    assert evaluate_security_expression("hasPermission(#doc, 'read')", ctx, args={"doc": "other"}) is False


def test_without_evaluator_falls_back_to_context_permission() -> None:
    ctx = SecurityContext(user_id="u", permissions=["read"])
    # No evaluator installed: target is ignored, the permission is checked on the context.
    assert evaluate_security_expression("hasPermission(#doc, 'read')", ctx, args={"doc": "x"}) is True
    assert evaluate_security_expression("hasPermission(#doc, 'write')", ctx, args={"doc": "x"}) is False


def test_three_arg_form_passes_target_type() -> None:
    captured: dict[str, Any] = {}

    class _Capture:
        def has_permission(self, context: Any, target: Any, permission: str, *, target_type: str | None = None) -> bool:
            captured.update(target=target, permission=permission, target_type=target_type)
            return True

    set_permission_evaluator(_Capture())
    ctx = SecurityContext(user_id="u")
    assert evaluate_security_expression("hasPermission(#id, 'Document', 'read')", ctx, args={"id": "7"}) is True
    assert captured == {"target": "7", "permission": "read", "target_type": "Document"}
