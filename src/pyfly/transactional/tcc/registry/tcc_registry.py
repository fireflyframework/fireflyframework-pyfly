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
"""TCC registry — discovery, indexing and validation of TCC beans."""

from __future__ import annotations

import inspect
from typing import Any

from pyfly.transactional.tcc.registry.participant_definition import (
    ParticipantDefinition,
)
from pyfly.transactional.tcc.registry.tcc_definition import TccDefinition


class TccValidationError(Exception):
    """Raised when a TCC definition fails structural validation.

    Typical causes include duplicate participant ids or participants
    that are missing a required ``@try_method``.
    """


class TccRegistry:
    """Discovery and indexing service for ``@tcc``-decorated beans.

    Scans bean classes for ``__pyfly_tcc__`` metadata, discovers nested classes
    carrying ``__pyfly_tcc_participant__`` metadata, resolves ``@try_method``,
    ``@confirm_method`` and ``@cancel_method`` on each participant, validates
    the definitions and stores the resulting :class:`TccDefinition`.
    """

    def __init__(self) -> None:
        self._tccs: dict[str, TccDefinition] = {}

    # -- Public API ----------------------------------------------------------

    def register_from_bean(self, bean: Any) -> TccDefinition:
        """Register a TCC transaction from a decorated bean instance.

        Inspects the bean's class for ``__pyfly_tcc__`` metadata, discovers
        all nested classes carrying ``__pyfly_tcc_participant__`` metadata,
        resolves try/confirm/cancel methods on each participant, validates
        uniqueness and required methods, and sorts participants by order.

        Args:
            bean: An instance of a ``@tcc``-decorated class.

        Returns:
            The fully populated :class:`TccDefinition`.

        Raises:
            TccValidationError: If the bean is not decorated with ``@tcc``,
                if participant ids are duplicated, or if a participant lacks
                a ``@try_method``.
        """
        cls = type(bean)
        tcc_meta: dict[str, Any] = getattr(cls, "__pyfly_tcc__", None)  # type: ignore[assignment]
        if tcc_meta is None:
            msg = f"{cls.__qualname__} is not decorated with @tcc"
            raise TccValidationError(msg)

        tcc_name: str = tcc_meta["name"]

        definition = TccDefinition(
            name=tcc_name,
            bean=bean,
            timeout_ms=tcc_meta.get("timeout_ms", 0),
            retry_enabled=tcc_meta.get("retry_enabled", False),
            max_retries=tcc_meta.get("max_retries", 0),
            backoff_ms=tcc_meta.get("backoff_ms", 0),
        )

        # Collect unsorted participant definitions first, then sort by order.
        unsorted_participants: list[ParticipantDefinition] = []

        # Discover participants by iterating over class members looking for
        # nested classes with __pyfly_tcc_participant__ metadata.
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name, None)
            if attr is None or not inspect.isclass(attr):
                continue
            participant_meta: dict[str, Any] | None = getattr(attr, "__pyfly_tcc_participant__", None)
            if participant_meta is None:
                continue

            participant_id: str = participant_meta["id"]

            # Check for duplicate participant ids.
            if participant_id in {p.id for p in unsorted_participants}:
                msg = f"Duplicate participant id '{participant_id}' in TCC '{tcc_name}'"
                raise TccValidationError(msg)

            # Resolve try/confirm/cancel methods on the participant class.
            resolved_try: Any | None = None
            resolved_confirm: Any | None = None
            resolved_cancel: Any | None = None

            for method_name in dir(attr):
                method = getattr(attr, method_name, None)
                if method is None:
                    continue

                if getattr(method, "__pyfly_try_method__", None) is not None:
                    resolved_try = method
                elif getattr(method, "__pyfly_confirm_method__", None) is not None:
                    resolved_confirm = method
                elif getattr(method, "__pyfly_cancel_method__", None) is not None:
                    resolved_cancel = method

            # Validate that the participant has at least a try method.
            if resolved_try is None:
                msg = f"Participant '{participant_id}' in TCC '{tcc_name}' must have a @try_method"
                raise TccValidationError(msg)

            participant_def = ParticipantDefinition(
                id=participant_id,
                order=participant_meta.get("order", 0),
                timeout_ms=participant_meta.get("timeout_ms", 0),
                optional=participant_meta.get("optional", False),
                participant_class=attr,
                try_method=resolved_try,
                confirm_method=resolved_confirm,
                cancel_method=resolved_cancel,
            )
            unsorted_participants.append(participant_def)

        # Sort participants by order and populate the definition.
        unsorted_participants.sort(key=lambda p: p.order)
        for p in unsorted_participants:
            definition.participants[p.id] = p

        self._tccs[tcc_name] = definition
        return definition

    def get(self, name: str) -> TccDefinition | None:
        """Look up a TCC definition by name.

        Args:
            name: The TCC name supplied to ``@tcc(name=...)``.

        Returns:
            The :class:`TccDefinition` if registered, otherwise ``None``.
        """
        return self._tccs.get(name)

    def get_all(self) -> list[TccDefinition]:
        """Return all registered TCC definitions.

        Returns:
            A list of all :class:`TccDefinition` instances currently
            registered in this registry.
        """
        return list(self._tccs.values())
