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
"""TCC argument resolver — resolves ``Annotated`` parameters from execution context."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Annotated, Any, get_args, get_origin, get_type_hints

from pyfly.transactional.saga.annotations import Input
from pyfly.transactional.tcc.annotations import FromTry
from pyfly.transactional.tcc.core.context import TccContext

# Sentinel for "marker not recognised".
_UNRESOLVED = object()


class TccArgumentResolver:
    """Resolves TCC participant method parameters from execution context.

    Inspects type hints (including ``Annotated`` extras) to determine how each
    parameter should be supplied.  The resolver handles:

    * ``TccContext`` -- injected by type match
    * ``Annotated[T, Input()]`` -- entire step input
    * ``Annotated[T, Input("key")]`` -- specific key from step input
    * ``Annotated[T, FromTry()]`` -- try result for the current participant
    * ``self`` -- skipped
    """

    def resolve(
        self,
        func: Callable[..., Any],
        bean: Any,
        ctx: TccContext,
        input_data: Any = None,
        participant_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve all parameters for a participant method.

        Parameters
        ----------
        func:
            The participant method whose parameters should be resolved.
        bean:
            The TCC instance that owns *func* (used only for ``self``
            detection).
        ctx:
            The current TCC execution context.
        input_data:
            The input payload for this TCC, if any.
        participant_id:
            The participant ID (needed for ``FromTry`` resolution).

        Returns
        -------
        dict[str, Any]
            Dictionary mapping parameter names to their resolved values.

        Raises
        ------
        TypeError
            If a parameter cannot be resolved from any known source.
        """
        hints = get_type_hints(func, include_extras=True)
        sig = inspect.signature(func)

        resolved: dict[str, Any] = {}

        for name in sig.parameters:
            if name == "self":
                continue

            hint = hints.get(name)
            if hint is None:
                raise TypeError(
                    f"Cannot resolve parameter '{name}' — no type hint found on {func.__qualname__}",
                )

            value = self._resolve_parameter(
                name,
                hint,
                ctx,
                input_data,
                participant_id,
            )
            resolved[name] = value

        return resolved

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_parameter(
        self,
        name: str,
        hint: Any,
        ctx: TccContext,
        input_data: Any,
        participant_id: str | None,
    ) -> Any:
        """Resolve a single parameter from its type hint."""
        # 1. Direct TccContext type match (no Annotated wrapper needed).
        if self._is_tcc_context(hint):
            return ctx

        # 2. Annotated[T, marker] — inspect extras for known markers.
        if get_origin(hint) is Annotated:
            args = get_args(hint)
            base_type = args[0]

            # Check base type for TccContext in case of Annotated[TccContext, ...]
            if self._is_tcc_context(base_type):
                return ctx

            for extra in args[1:]:
                result = self._resolve_marker(extra, ctx, input_data, participant_id)
                if result is not _UNRESOLVED:
                    return result

        # 3. Cannot resolve — raise.
        raise TypeError(
            f"Cannot resolve parameter '{name}' — no matching injection marker found in type hint: {hint}",
        )

    def _resolve_marker(
        self,
        marker: Any,
        ctx: TccContext,
        input_data: Any,
        participant_id: str | None,
    ) -> Any:
        """Attempt to resolve a value from a single annotation marker.

        Returns ``_UNRESOLVED`` if the marker is not recognised.
        """
        # Input() or Input("key")
        if isinstance(marker, Input):
            if marker.key is None:
                return input_data
            return self._extract_key(input_data, marker.key)

        # FromTry() — inject the try result for the current participant.
        if isinstance(marker, FromTry):
            if participant_id is not None:
                return ctx.get_try_result(participant_id)
            return None

        return _UNRESOLVED

    @staticmethod
    def _is_tcc_context(hint: Any) -> bool:
        """Return ``True`` if *hint* resolves to :class:`TccContext`."""
        if isinstance(hint, str):
            return hint == "TccContext"
        return hint is TccContext

    @staticmethod
    def _extract_key(obj: Any, key: str) -> Any:
        """Extract *key* from *obj* — supports dict subscript and attribute access."""
        if isinstance(obj, dict):
            return obj[key]
        return getattr(obj, key)
