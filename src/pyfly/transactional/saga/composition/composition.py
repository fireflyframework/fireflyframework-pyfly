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
"""Immutable composition types — SagaDataFlow, CompositionEntry, SagaComposition."""

from __future__ import annotations

from dataclasses import dataclass, field

from pyfly.transactional.shared.types import CompensationPolicy


@dataclass(frozen=True)
class SagaDataFlow:
    """Maps output from one saga to input of another.

    Parameters
    ----------
    source_saga:
        Name of the saga whose result provides the data.
    source_step:
        Specific step within the source saga.  ``None`` means use the
        entire :class:`SagaResult` of the source saga.
    target_key:
        Key under which the resolved value is placed in the target
        saga's input dict.  ``None`` means merge the resolved value
        directly (it must be a dict).
    """

    source_saga: str
    source_step: str | None = None
    target_key: str | None = None


@dataclass(frozen=True)
class CompositionEntry:
    """A saga within a composition.

    Parameters
    ----------
    saga_name:
        Registered name of the saga.
    depends_on:
        Names of other entries that must complete before this one.
    data_flows:
        Declarations describing how to wire output from upstream sagas
        into this saga's input.
    """

    saga_name: str
    depends_on: list[str] = field(default_factory=list)
    data_flows: list[SagaDataFlow] = field(default_factory=list)


@dataclass(frozen=True)
class SagaComposition:
    """Immutable definition of a multi-saga composition.

    A composition describes which sagas to run, their dependency order,
    and how data flows between them.  Entries are keyed by saga name.
    """

    name: str
    entries: dict[str, CompositionEntry] = field(default_factory=dict)
    compensation_policy: CompensationPolicy = CompensationPolicy.STRICT_SEQUENTIAL
