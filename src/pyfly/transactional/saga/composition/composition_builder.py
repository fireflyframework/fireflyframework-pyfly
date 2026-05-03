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
"""Fluent builder for SagaComposition definitions."""

from __future__ import annotations

from pyfly.transactional.saga.composition.composition import (
    CompositionEntry,
    SagaComposition,
    SagaDataFlow,
)
from pyfly.transactional.saga.composition.validator import CompositionValidator
from pyfly.transactional.shared.types import CompensationPolicy


class _EntryBuilder:
    """Builder for a single :class:`CompositionEntry` within the composition.

    Created by :meth:`SagaCompositionBuilder.saga` and finalised by
    :meth:`add`.
    """

    def __init__(self, parent: SagaCompositionBuilder, saga_name: str) -> None:
        self._parent = parent
        self._saga_name = saga_name
        self._depends_on: list[str] = []
        self._data_flows: list[SagaDataFlow] = []

    # ── fluent setters ────────────────────────────────────────

    def depends_on(self, *saga_names: str) -> _EntryBuilder:
        """Declare dependencies for this saga entry."""
        self._depends_on = list(saga_names)
        return self

    def data_flow(
        self,
        source_saga: str,
        source_step: str | None = None,
        target_key: str | None = None,
    ) -> _EntryBuilder:
        """Declare a data-flow mapping from an upstream saga."""
        self._data_flows.append(
            SagaDataFlow(
                source_saga=source_saga,
                source_step=source_step,
                target_key=target_key,
            ),
        )
        return self

    # ── finalise ──────────────────────────────────────────────

    def add(self) -> SagaCompositionBuilder:
        """Finalise this entry and return to the parent builder."""
        entry = CompositionEntry(
            saga_name=self._saga_name,
            depends_on=self._depends_on,
            data_flows=self._data_flows,
        )
        self._parent._entries[self._saga_name] = entry  # noqa: SLF001
        return self._parent


class SagaCompositionBuilder:
    """Fluent builder that produces a validated :class:`SagaComposition`.

    Example::

        composition = (
            SagaCompositionBuilder("order-fulfillment")
            .saga("reserve-inventory").depends_on().add()
            .saga("process-payment").depends_on("reserve-inventory").add()
            .saga("ship-order").depends_on("process-payment").add()
            .compensation_policy(CompensationPolicy.GROUPED_PARALLEL)
            .build()
        )
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._entries: dict[str, CompositionEntry] = {}
        self._compensation_policy = CompensationPolicy.STRICT_SEQUENTIAL

    # ── fluent API ────────────────────────────────────────────

    def saga(self, saga_name: str) -> _EntryBuilder:
        """Begin defining an entry for the named saga."""
        return _EntryBuilder(self, saga_name)

    def compensation_policy(self, policy: CompensationPolicy) -> SagaCompositionBuilder:
        """Set the compensation policy used when the composition fails."""
        self._compensation_policy = policy
        return self

    # ── build ─────────────────────────────────────────────────

    def build(self) -> SagaComposition:
        """Build and validate the :class:`SagaComposition`.

        Raises
        ------
        ValueError
            If the composition has no entries or fails validation.
        """
        if not self._entries:
            msg = "Composition must contain at least one saga entry"
            raise ValueError(msg)

        composition = SagaComposition(
            name=self._name,
            entries=dict(self._entries),
            compensation_policy=self._compensation_policy,
        )
        CompositionValidator.validate(composition)
        return composition
