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
"""Tests for is_expected_error (4xx vs 5xx classification)."""

from __future__ import annotations

from pyfly.kernel.exceptions import (
    InfrastructureException,
    ResourceNotFoundException,
    SecurityException,
    UnauthorizedException,
    ValidationException,
    is_expected_error,
)


def test_business_and_validation_are_expected() -> None:
    assert is_expected_error(ValidationException("bad input"))
    assert is_expected_error(ResourceNotFoundException("missing"))


def test_security_is_expected() -> None:
    assert is_expected_error(SecurityException("denied"))
    assert is_expected_error(UnauthorizedException("no token"))


def test_infrastructure_and_unexpected_are_not_expected() -> None:
    assert not is_expected_error(InfrastructureException("db down"))
    assert not is_expected_error(RuntimeError("boom"))
    assert not is_expected_error(ValueError("nope"))
