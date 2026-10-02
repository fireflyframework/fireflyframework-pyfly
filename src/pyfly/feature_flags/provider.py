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
"""``FireflyFlagProvider``: the OpenFeature provider over flagd's reference in-process evaluator.

Evaluation is ``openfeature-flagd-core``'s ``FlagdCore`` (spec D3). The provider adds what the framework needs around
it: a whole-document :meth:`FireflyFlagProvider.update` that reports the changed keys and emits
``PROVIDER_CONFIGURATION_CHANGED``, and a projection of every definition onto the five flagd fields, because
``FlagdCore`` builds ``Flag(**definition)`` and an unknown key (``description`` beside ``state``) would make it refuse
the whole document.
"""

from __future__ import annotations

import copy
import json
import threading
from collections.abc import Mapping, Sequence
from typing import Any

from openfeature.contrib.tools.flagd.core import FlagdCore
from openfeature.evaluation_context import EvaluationContext
from openfeature.event import ProviderEventDetails
from openfeature.flag_evaluation import FlagResolutionDetails, FlagValueType
from openfeature.provider import AbstractProvider, Metadata

__all__ = ["PROVIDER_NAME", "FireflyFlagProvider"]

PROVIDER_NAME = "firefly"
"""The provider's name in OpenFeature metadata, the actuator and the health details."""

_FLAG_FIELDS = ("state", "variants", "defaultVariant", "targeting", "metadata")


def _project(document: Mapping[str, Any]) -> dict[str, Any]:
    """*document* reduced to what FlagdCore reads, deep-copied."""
    flags = document.get("flags") or {}
    projected: dict[str, Any] = {
        "flags": {
            str(key): {name: copy.deepcopy(definition[name]) for name in _FLAG_FIELDS if name in definition}
            for key, definition in flags.items()
            if isinstance(definition, Mapping)
        }
    }
    if document.get("$evaluators"):
        projected["$evaluators"] = copy.deepcopy(dict(document["$evaluators"]))
    if document.get("metadata"):
        projected["metadata"] = copy.deepcopy(dict(document["metadata"]))
    return projected


class FireflyFlagProvider(AbstractProvider):
    """Evaluates the composed flag document; :meth:`update` replaces it as a whole."""

    def __init__(self) -> None:
        super().__init__()
        self._core = FlagdCore()
        self._lock = threading.Lock()
        self._document: dict[str, Any] = {"flags": {}}

    def get_metadata(self) -> Metadata:
        return Metadata(name=PROVIDER_NAME)

    @property
    def document(self) -> dict[str, Any]:
        """The flagd document being evaluated (a deep copy)."""
        with self._lock:
            return copy.deepcopy(self._document)

    def definition(self, key: str) -> dict[str, Any] | None:
        """The definition of *key* being evaluated (a deep copy), or ``None``."""
        with self._lock:
            found = self._document["flags"].get(key)
            return copy.deepcopy(found) if found is not None else None

    def update(self, document: Mapping[str, Any]) -> list[str]:
        """Evaluate *document* from now on; returns the keys whose definition changed, sorted.

        FlagdCore keeps its previous flags when it refuses a document (it raises before replacing them), so a
        failed update leaves the provider as it was.
        """
        projected = _project(document)
        with self._lock:
            # A JSON string, never the dicts: FlagdCore pops ``defaultVariant`` out of what it is given.
            changed = sorted(self._core.set_flags_and_get_changed_keys(json.dumps(projected)))
            self._document = projected
        if changed:
            self.emit_provider_configuration_changed(ProviderEventDetails(flags_changed=changed))
        return changed

    def shutdown(self) -> None:
        """Keep the document: the SDK shuts a replaced provider down on a thread while its registry may still use it."""

    def resolve_boolean_details(
        self, flag_key: str, default_value: bool, evaluation_context: EvaluationContext | None = None
    ) -> FlagResolutionDetails[bool]:
        return self._core.resolve_boolean_value(flag_key, default_value, evaluation_context)

    def resolve_string_details(
        self, flag_key: str, default_value: str, evaluation_context: EvaluationContext | None = None
    ) -> FlagResolutionDetails[str]:
        return self._core.resolve_string_value(flag_key, default_value, evaluation_context)

    def resolve_integer_details(
        self, flag_key: str, default_value: int, evaluation_context: EvaluationContext | None = None
    ) -> FlagResolutionDetails[int]:
        return self._core.resolve_integer_value(flag_key, default_value, evaluation_context)

    def resolve_float_details(
        self, flag_key: str, default_value: float, evaluation_context: EvaluationContext | None = None
    ) -> FlagResolutionDetails[float]:
        return self._core.resolve_float_value(flag_key, default_value, evaluation_context)

    def resolve_object_details(
        self,
        flag_key: str,
        default_value: Sequence[FlagValueType] | Mapping[str, FlagValueType],
        evaluation_context: EvaluationContext | None = None,
    ) -> FlagResolutionDetails[Sequence[FlagValueType] | Mapping[str, FlagValueType]]:
        return self._core.resolve_object_value(flag_key, default_value, evaluation_context)
