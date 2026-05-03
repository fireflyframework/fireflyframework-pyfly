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
"""Resolves data flows between sagas in a composition."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.transactional.saga.composition.composition import CompositionEntry
    from pyfly.transactional.saga.composition.composition_context import (
        CompositionContext,
    )


class DataFlowManager:
    """Resolves inputs for a saga entry based on its data-flow declarations.

    For each :class:`SagaDataFlow` in the entry:

    * If ``source_step`` is set, extracts the result of that specific step
      from the source saga's :class:`SagaResult`.
    * If ``source_step`` is ``None``, uses the entire :class:`SagaResult`.
    * If ``target_key`` is set, places the resolved value under that key in
      the result dict.
    * If ``target_key`` is ``None`` and the resolved value is a dict, it is
      merged directly into the result dict.
    """

    @staticmethod
    def resolve_input(
        entry: CompositionEntry,
        ctx: CompositionContext,
        initial_input: Any,
    ) -> Any:
        """Build the input for a saga from its data-flow declarations.

        Parameters
        ----------
        entry:
            The composition entry whose input is being resolved.
        ctx:
            The current composition context containing results from
            already-completed sagas.
        initial_input:
            The initial input provided to the composition.  This serves
            as the base onto which data-flow values are layered.

        Returns
        -------
        Any
            The resolved input value (typically a dict).
        """
        if not entry.data_flows:
            return initial_input

        # Start from a copy of initial_input if it is a dict, otherwise
        # create a fresh dict.
        if isinstance(initial_input, dict):
            resolved: dict[str, Any] = dict(initial_input)
        else:
            resolved = {}

        for flow in entry.data_flows:
            source_result = ctx.saga_results.get(flow.source_saga)
            if source_result is None:
                continue

            # Determine the value to inject.
            value = source_result.result_of(flow.source_step) if flow.source_step is not None else source_result

            # Place into the resolved dict.
            if flow.target_key is not None:
                resolved[flow.target_key] = value
            elif isinstance(value, dict):
                resolved.update(value)
            else:
                resolved[flow.source_saga] = value

        return resolved
