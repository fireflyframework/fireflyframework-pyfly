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
"""Validates a SagaComposition for structural correctness."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyfly.transactional.saga.engine.topology import SagaTopology

if TYPE_CHECKING:
    from pyfly.transactional.saga.composition.composition import SagaComposition


class CompositionValidator:
    """Static validator for :class:`SagaComposition` definitions.

    Checks:
    - All ``depends_on`` references point to entries that exist.
    - All ``data_flow.source_saga`` references point to entries that exist.
    - The dependency graph is acyclic (delegated to :class:`SagaTopology`).
    """

    @staticmethod
    def validate(composition: SagaComposition) -> None:
        """Validate the composition, raising :class:`ValueError` on any issue.

        Parameters
        ----------
        composition:
            The composition to validate.

        Raises
        ------
        ValueError
            If any dependency reference is missing, a data-flow source is
            missing, or the dependency graph contains a cycle.
        """
        entry_names = set(composition.entries)

        # -- 1. Check depends_on references ----------------------------------
        for name, entry in composition.entries.items():
            for dep in entry.depends_on:
                if dep not in entry_names:
                    msg = f"Entry '{name}' depends on '{dep}' which does not exist in composition '{composition.name}'"
                    raise ValueError(msg)

        # -- 2. Check data_flow source_saga references -----------------------
        for name, entry in composition.entries.items():
            for flow in entry.data_flows:
                if flow.source_saga not in entry_names:
                    msg = (
                        f"Entry '{name}' has a data flow referencing source "
                        f"saga '{flow.source_saga}' which does not exist in "
                        f"composition '{composition.name}'"
                    )
                    raise ValueError(msg)

        # -- 3. Cycle detection via SagaTopology -----------------------------
        deps: dict[str, list[str]] = {name: list(entry.depends_on) for name, entry in composition.entries.items()}
        SagaTopology.compute_layers(deps)  # raises ValueError on cycle
