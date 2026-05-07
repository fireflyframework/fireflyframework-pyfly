# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for OrchestrationValidator."""

from __future__ import annotations

import pytest

from pyfly.transactional.core.exceptions import OrchestrationValidationError
from pyfly.transactional.core.validator import OrchestrationValidator


def test_dag_valid_returns_no_errors() -> None:
    v = OrchestrationValidator()
    report = v.validate_dag("test", {"a": [], "b": ["a"]})
    assert not report.has_errors


def test_dag_with_cycle_reports_error() -> None:
    v = OrchestrationValidator()
    report = v.validate_dag("test", {"a": ["b"], "b": ["a"]})
    assert report.has_errors
    with pytest.raises(OrchestrationValidationError):
        report.raise_if_errors()


def test_dag_with_missing_dependency_reports_error() -> None:
    v = OrchestrationValidator()
    report = v.validate_dag("test", {"a": ["nope"]})
    assert report.has_errors


def test_empty_graph_reports_error() -> None:
    v = OrchestrationValidator()
    report = v.validate_dag("test", {})
    assert report.has_errors
