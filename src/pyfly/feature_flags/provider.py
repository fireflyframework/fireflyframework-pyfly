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
:data:`EXPANSION_LIMIT` JSON values or nest deeper than :data:`DEPTH_LIMIT` levels (its whole targeting becomes an
unresolvable reference): FlagdCore knows no ``$ref`` operation, so evaluating that flag is a ``PARSE_ERROR`` while
the rest of the document loads. Every reference resolved along the way costs one unit of the value budget too
(R-ref-budget), so references to references are bounded like everything else. Both limits are decided in one pass
that spends at most the budget and never recurses deeper than the depth limit (a reference to a reference is
followed in a loop), so neither a fan-out nor a long chain of references can exhaust time, memory or the stack; only
a flag within both limits is expanded.

Date-times in the evaluation context are evaluated as Unix epoch milliseconds (a ``float``: the whole microseconds
since the epoch, divided by 1000, exactly as PHP computes it), whatever the host's time zone (the contract's
"Evaluation context"): every ``resolve_*_details`` converts the context it is handed, the merged OpenFeature context,
before FlagdCore reads it, so facade calls, plain OpenFeature clients and the management preview decide alike. See
:func:`_epoch_millis_context`.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import threading
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from openfeature.contrib.tools.flagd.core import FlagdCore
from openfeature.evaluation_context import EvaluationContext
from openfeature.event import ProviderEventDetails
from openfeature.flag_evaluation import FlagResolutionDetails, FlagValueType
from openfeature.provider import AbstractProvider, Metadata

from pyfly.feature_flags._references import referenced_evaluator

__all__ = ["DEPTH_LIMIT", "EXPANSION_LIMIT", "PROVIDER_NAME", "FireflyFlagProvider"]

PROVIDER_NAME = "firefly"
"""The provider's name in OpenFeature metadata, the actuator and the health details."""

EXPANSION_LIMIT = 10_000
"""The most JSON values a flag's targeting may expand to; every object, array and scalar counts one, and so does
every reference resolved along the way (spec 4.1)."""

DEPTH_LIMIT = 128
"""The most nesting levels a flag's expanded targeting may have (spec 4.1): the targeting object is level 1, each
object or array inside a container adds one, and a resolved reference takes the place of its ``{"$ref": ...}`` object
without adding a level."""

_FLAG_FIELDS = ("state", "variants", "defaultVariant", "targeting", "metadata")

_OVER_THE_LIMIT = "$firefly-expansion-limit"
"""The evaluator a flag over :data:`EXPANSION_LIMIT` or :data:`DEPTH_LIMIT` is made to reference. FlagdCore is handed
no ``$evaluators``, so the reference stays unresolved whatever the document's evaluators are named."""


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


def _follow(node: Any, evaluators: Mapping[str, Any], resolving: set[str], most: int) -> tuple[Any, list[str]]:
    """Follow *node* while it is a resolvable reference, at most *most* of them, in a loop: the node reached, and the
    evaluator names entered on the way, now in *resolving* (the caller removes them once that node is done).

    A reference is resolvable when its evaluator exists and is not already being resolved on this path (a cycle).
    """
    entered: list[str] = []
    while (
        len(entered) < most
        and (name := referenced_evaluator(node)) is not None
        and name in evaluators
        and name not in resolving
    ):
        resolving.add(name)
        entered.append(name)
        node = evaluators[name]
    return node, entered


def _over_the_limits(targeting: Any, evaluators: Mapping[str, Any]) -> bool:
    """Whether *targeting*, its references expanded, holds more than :data:`EXPANSION_LIMIT` JSON values or nests
    deeper than :data:`DEPTH_LIMIT` levels.

    Counts without building the expansion and stops as soon as a limit is passed. Every step spends a unit of the
    budget (a value, or a reference resolved: an unresolved one counts as the object it is), so a flag costs at most
    :data:`EXPANSION_LIMIT` steps however its references fan out (``2**20`` of them in the vectors) or chain. It
    recurses once per level and stops at a container past the depth limit, so it never exhausts the stack either.
    """
    budget = EXPANSION_LIMIT
    resolving: set[str] = set()

    def over(node: Any, level: int) -> bool:
        nonlocal budget
        node, entered = _follow(node, evaluators, resolving, budget + 1)
        try:
            budget -= len(entered) + 1  # each reference resolved, then the value it stands for
            if budget < 0:
                return True
            if isinstance(node, Mapping):
                children: Iterable[Any] = node.values()
            elif isinstance(node, list | tuple):
                children = node
            else:
                return False  # a scalar adds no level
            if level > DEPTH_LIMIT:
                return True
            return any(over(child, level + 1) for child in children)
        finally:
            resolving.difference_update(entered)

    return over(targeting, 1)


def _expand(node: Any, evaluators: Mapping[str, Any], resolving: set[str]) -> Any:
    """*node* with every resolvable reference replaced by its expanded evaluator: a new tree, strings untouched, and a
    missing name or a cycle left as its ``{"$ref": name}`` object. Only called within both limits, so it recurses at
    most :data:`DEPTH_LIMIT` levels and resolves fewer than :data:`EXPANSION_LIMIT` references."""
    node, entered = _follow(node, evaluators, resolving, EXPANSION_LIMIT)
    try:
        if isinstance(node, Mapping):
            return {key: _expand(value, evaluators, resolving) for key, value in node.items()}
        if isinstance(node, list | tuple):
            return [_expand(item, evaluators, resolving) for item in node]
        return node
    finally:
        resolving.difference_update(entered)


def _for_flagd_core(projected: Mapping[str, Any]) -> dict[str, Any]:
    """The document FlagdCore is handed: every ``targeting`` expanded, no ``$evaluators`` left to substitute."""
    evaluators: Mapping[str, Any] = projected.get("$evaluators") or {}
    flags: dict[str, Any] = {}
    for key, definition in projected["flags"].items():
        flag = dict(definition)
        if "targeting" in flag:
            if _over_the_limits(flag["targeting"], evaluators):
                flag["targeting"] = {"$ref": _OVER_THE_LIMIT}
            else:
                flag["targeting"] = _expand(flag["targeting"], evaluators, set())
        flags[key] = flag
    document: dict[str, Any] = {"flags": flags}
    if "metadata" in projected:
        document["metadata"] = projected["metadata"]
    return document


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


def _epoch_millis(value: date) -> float:
    """*value* as Unix epoch milliseconds: the whole number of microseconds since the epoch (an ``int``, floored, so
    negative before 1970), divided by 1000 as a float. PHP computes the same integer and the same division, so both
    frameworks produce bit-identical doubles (``timestamp() * 1000.0`` can differ in the last bit). A naive
    ``datetime`` is read as UTC (never the host's zone) and a ``date`` is midnight UTC."""
    if not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day, tzinfo=UTC)
    elif value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return ((value - _EPOCH) // _MICROSECOND) / 1000


def _epoch_millis_context(context: EvaluationContext | None) -> EvaluationContext | None:
    """*context* with every ``datetime`` and ``date`` value, at any depth inside mappings, lists and tuples, replaced by
    its epoch milliseconds (:func:`_epoch_millis`); anything else is untouched, strings that look like dates included.

    The caller's context is never mutated: a container holding a date-time is rebuilt (a ``dict``, a ``list``, a
    ``tuple``), one that holds none is shared as it is, and a context without any date-time is returned as it is.

    The walk is bounded the way a flag's expanded targeting is: containers more than :data:`DEPTH_LIMIT` levels deep
    (the attributes are level 1) are not entered, and after :data:`EXPANSION_LIMIT` values the rest is left as it is,
    so neither a deep nesting, nor a structure that refers to itself or shares its parts over and over, can exhaust
    time or the stack. A date-time out there is not converted: such a context is not a JSON value.
    """
    if context is None:
        return None
    budget = EXPANSION_LIMIT

    def walk(node: Any, level: int) -> Any:
        nonlocal budget
        budget -= 1
        if budget < 0:
            return node
        if isinstance(node, date):  # a datetime is a date
            return _epoch_millis(node)
        if level > DEPTH_LIMIT:
            return node
        if isinstance(node, Mapping):
            changed = {key: new for key, value in node.items() if (new := walk(value, level + 1)) is not value}
            return {**node, **changed} if changed else node
        if isinstance(node, list | tuple):
            items = [walk(item, level + 1) for item in node]
            if all(new is old for new, old in zip(items, node, strict=True)):
                return node
            return items if isinstance(node, list) else tuple(items)
        return node

    attributes = walk(context.attributes, 1)
    return context if attributes is context.attributes else dataclasses.replace(context, attributes=attributes)


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

    def update(self, document: Mapping[str, Any], *, changed_keys: Sequence[str] | None = None) -> list[str]:
        """Evaluate *document* from now on; returns FlagdCore's own diff: the keys whose flagd definition changed
        (the five fields, references expanded; document ``metadata`` is not compared), sorted.

        ``PROVIDER_CONFIGURATION_CHANGED`` is emitted when keys changed: *changed_keys* (sorted) when given, which is
        how the registry makes OpenFeature handlers see the keys ``FeatureFlagsChanged`` carries; FlagdCore's diff
        otherwise. An empty *changed_keys* emits nothing. The return value is FlagdCore's diff either way.

        FlagdCore keeps its previous flags when it refuses a document (it raises before replacing them), so a
        failed update leaves the provider as it was.
        """
        projected = _project(document)
        # A JSON string, never the dicts: FlagdCore pops ``defaultVariant`` out of what it is given.
        expanded = json.dumps(_for_flagd_core(projected))
        with self._lock:
            changed = sorted(self._core.set_flags_and_get_changed_keys(expanded))
            self._document = projected
        signalled = changed if changed_keys is None else sorted(changed_keys)
        if signalled:
            self.emit_provider_configuration_changed(ProviderEventDetails(flags_changed=signalled))
        return changed

    def shutdown(self) -> None:
        """Keep the document: the SDK shuts a replaced provider down on a thread while its registry may still use it."""

    def resolve_boolean_details(
        self, flag_key: str, default_value: bool, evaluation_context: EvaluationContext | None = None
    ) -> FlagResolutionDetails[bool]:
        return self._core.resolve_boolean_value(flag_key, default_value, _epoch_millis_context(evaluation_context))

    def resolve_string_details(
        self, flag_key: str, default_value: str, evaluation_context: EvaluationContext | None = None
    ) -> FlagResolutionDetails[str]:
        return self._core.resolve_string_value(flag_key, default_value, _epoch_millis_context(evaluation_context))

    def resolve_integer_details(
        self, flag_key: str, default_value: int, evaluation_context: EvaluationContext | None = None
    ) -> FlagResolutionDetails[int]:
        return self._core.resolve_integer_value(flag_key, default_value, _epoch_millis_context(evaluation_context))

    def resolve_float_details(
        self, flag_key: str, default_value: float, evaluation_context: EvaluationContext | None = None
    ) -> FlagResolutionDetails[float]:
        return self._core.resolve_float_value(flag_key, default_value, _epoch_millis_context(evaluation_context))

    def resolve_object_details(
        self,
        flag_key: str,
        default_value: Sequence[FlagValueType] | Mapping[str, FlagValueType],
        evaluation_context: EvaluationContext | None = None,
    ) -> FlagResolutionDetails[Sequence[FlagValueType] | Mapping[str, FlagValueType]]:
        return self._core.resolve_object_value(flag_key, default_value, _epoch_millis_context(evaluation_context))
