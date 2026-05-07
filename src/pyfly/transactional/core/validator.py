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
"""Startup-time validation of saga / workflow / TCC definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pyfly.transactional.core.exceptions import OrchestrationValidationError
from pyfly.transactional.core.topology import TopologyBuilder, TopologyError


class IssueLevel(StrEnum):
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass
class ValidationIssue:
    """A single validation problem found in a definition."""

    target: str
    level: IssueLevel
    message: str


@dataclass
class ValidationReport:
    """Aggregated result of running the validator."""

    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(i.level == IssueLevel.ERROR for i in self.issues)

    def raise_if_errors(self) -> None:
        if self.has_errors:
            errs = "; ".join(f"[{i.target}] {i.message}" for i in self.issues if i.level == IssueLevel.ERROR)
            raise OrchestrationValidationError(errs)


class OrchestrationValidator:
    """Validate the structural integrity of orchestration definitions."""

    def __init__(self, *, fail_on_warning: bool = False) -> None:
        self._fail_on_warning = fail_on_warning

    def validate_dag(
        self,
        target: str,
        graph: dict[str, list[str]],
    ) -> ValidationReport:
        """Validate that *graph* is a well-formed DAG."""
        report = ValidationReport()
        if not graph:
            report.issues.append(
                ValidationIssue(target=target, level=IssueLevel.ERROR, message="no steps defined"),
            )
            return report
        try:
            TopologyBuilder.build_layers(graph)
        except TopologyError as exc:
            report.issues.append(ValidationIssue(target=target, level=IssueLevel.ERROR, message=str(exc)))
        return report

    def fail_if_needed(self, report: ValidationReport) -> None:
        if self._fail_on_warning and report.issues:
            errs = "; ".join(f"[{i.target}] {i.message}" for i in report.issues)
            raise OrchestrationValidationError(errs)
        report.raise_if_errors()
