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
``PROVIDER_CONFIGURATION_CHANGED``, a projection of every definition onto the five flagd fields, because
``FlagdCore`` builds ``Flag(**definition)`` and an unknown key (``description`` beside ``state``) would make it refuse
the whole document, and the contract's ``$ref`` resolution (spec 4.1).

``$ref`` resolution: FlagdCore substitutes references textually, with the rule's JSON as an ``re.sub`` template (a
backslash in an evaluator is re-escaped: ``"C:\\temp"`` becomes a tab, ``"a\\d"`` refuses the whole document), in
one pass that never resolves a reference an evaluator brings in. The provider therefore expands every flag's
``targeting`` on the parsed document and hands FlagdCore a document without ``$evaluators``: a ``{"$ref": name}``
object (one key, a text value) is replaced by the named evaluator, itself expanded, whatever the names sort as.
A missing name or a cycle leaves the reference in place, and so does a flag whose targeting would expand to more than
:data:`EXPANSION_LIMIT` JSON values (its whole targeting becomes an unresolvable reference): FlagdCore knows no
``$ref`` operation, so evaluating that flag is a ``PARSE_ERROR`` while the rest of the document loads.
"""

from __future__ import annotations

import copy
import json
import threading
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from openfeature.contrib.tools.flagd.core import FlagdCore
from openfeature.evaluation_context import EvaluationContext
from openfeature.event import ProviderEventDetails
from openfeature.flag_evaluation import FlagResolutionDetails, FlagValueType
from openfeature.provider import AbstractProvider, Metadata

__all__ = ["EXPANSION_LIMIT", "PROVIDER_NAME", "FireflyFlagProvider"]

PROVIDER_NAME = "firefly"
"""The provider's name in OpenFeature metadata, the actuator and the health details."""

EXPANSION_LIMIT = 10_000
"""The most JSON values a flag's targeting may expand to; every object, array and scalar counts one (spec 4.1)."""

_FLAG_FIELDS = ("state", "variants", "defaultVariant", "targeting", "metadata")

_OVER_THE_LIMIT = "$firefly-expansion-limit"
"""The evaluator a flag over :data:`EXPANSION_LIMIT` is made to reference. FlagdCore is handed no ``$evaluators``, so
the reference stays unresolved whatever the document's evaluators are named."""


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


def _reference(node: Any) -> str | None:
    """The evaluator *node* references when it is a ``{"$ref": name}`` object (exactly one key, a text value)."""
    if isinstance(node, Mapping) and len(node) == 1:
        name = node.get("$ref")
        if isinstance(name, str):
            return name
    return None


def _exceeds(targeting: Any, evaluators: Mapping[str, Any], limit: int) -> bool:
    """Whether *targeting*, its references expanded, holds more than *limit* JSON values.

    Counts without building the expansion and stops as soon as the budget is spent, so a fan-out of references
    (``2**20`` of them in the vectors) costs about *limit* steps. An unresolved reference counts as the object it is.
    """
    budget = limit

    def count(node: Any, resolving: frozenset[str]) -> None:
        nonlocal budget
        name = _reference(node)
        if name is not None and name in evaluators and name not in resolving:
            count(evaluators[name], resolving | {name})
            return
        budget -= 1
        if isinstance(node, Mapping):
            children: Iterable[Any] = node.values()
        elif isinstance(node, list | tuple):
            children = node
        else:
            return
        for child in children:
            if budget < 0:
                return
            count(child, resolving)

    count(targeting, frozenset())
    return budget < 0


def _expand(node: Any, evaluators: Mapping[str, Any], resolving: frozenset[str]) -> Any:
    """*node* with every resolvable reference replaced by its expanded evaluator (a new tree; strings untouched)."""
    name = _reference(node)
    if name is not None:
        if name in evaluators and name not in resolving:
            return _expand(evaluators[name], evaluators, resolving | {name})
        return {"$ref": name}  # missing or a cycle: evaluating the flag is a PARSE_ERROR
    if isinstance(node, Mapping):
        return {key: _expand(value, evaluators, resolving) for key, value in node.items()}
    if isinstance(node, list | tuple):
        return [_expand(item, evaluators, resolving) for item in node]
    return node


def _for_flagd_core(projected: Mapping[str, Any]) -> dict[str, Any]:
    """The document FlagdCore is handed: every ``targeting`` expanded, no ``$evaluators`` left to substitute."""
    evaluators: Mapping[str, Any] = projected.get("$evaluators") or {}
    flags: dict[str, Any] = {}
    for key, definition in projected["flags"].items():
        flag = dict(definition)
        if "targeting" in flag:
            if _exceeds(flag["targeting"], evaluators, EXPANSION_LIMIT):
                flag["targeting"] = {"$ref": _OVER_THE_LIMIT}
            else:
                flag["targeting"] = _expand(flag["targeting"], evaluators, frozenset())
        flags[key] = flag
    document: dict[str, Any] = {"flags": flags}
    if "metadata" in projected:
        document["metadata"] = projected["metadata"]
    return document


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
        """The flagd document being evaluated (a deep copy): ``$evaluators`` and the references as written."""
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
        # A JSON string, never the dicts: FlagdCore pops ``defaultVariant`` out of what it is given.
        expanded = json.dumps(_for_flagd_core(projected))
        with self._lock:
            changed = sorted(self._core.set_flags_and_get_changed_keys(expanded))
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
