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
"""Firefly flag definitions: the shorthand, the contract's validation rules and expiry (spec 4.1, 4.2).

Everything here is plain Python over flagd documents, with no OpenFeature import, so sources, gating and test
support use it without the ``feature-flags`` extra. Validation reports the contract's messages (the shared vectors
assert them as substrings, and LaraFly emits the same phrases); a message may add a hint after the contract phrase.

An empty list (``[]``) where the contract expects an object (a flag's ``targeting`` or ``metadata``, the document's
``flags``, ``$evaluators`` or ``metadata``) stands for the empty object and is normalized to ``{}``: PHP cannot tell
the two apart in native configuration arrays, so both frameworks accept it. A non-empty list is still an error.

A definition nests at most :data:`MAX_DEFINITION_DEPTH` levels (``definition nests too deeply``). Every walk over a
definition keeps a stack of its own, so a document nested thousands of levels deep, or one that contains itself, is
refused without a ``RecursionError``.
"""

from __future__ import annotations

import copy
import datetime as dt
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "FLAG_KEY_PATTERN",
    "FLAG_KINDS",
    "MAX_DEFINITION_DEPTH",
    "FlagDefinitionError",
    "FlagDocument",
    "expired_keys",
    "flag_type",
    "is_expired",
    "is_valid_key",
    "normalize_flags",
    "parse_document",
    "utc_today",
    "validate_flag",
]

FLAG_KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
"""A flag key (spec 4.1), matched with ``fullmatch`` (``match`` would accept a trailing newline)."""

FLAG_KINDS: tuple[str, ...] = ("release", "experiment", "ops", "permission")
"""The values of the reserved ``metadata.kind``."""

MAX_DEFINITION_DEPTH = 256
"""The deepest a definition may nest (R-depth-validate): a flag definition, an evaluator rule or the document
``metadata`` is level 1 and every object or array inside adds one. Validation and parsing walk with a stack of their
own; the copies made downstream (composition, the provider, ``to_flagd``) are ``copy.deepcopy``, two frames a level, so
256 levels need about 520 of Python's default 1000: a definition that validates never exhausts the stack there."""

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _yaml_hint(names: str) -> str:
    """The hint appended where an unquoted YAML ``on``/``off`` key (read as a boolean) is the likely cause."""
    return f" (YAML 1.1 reads an unquoted on/off/yes/no as a boolean: quote {names} in pyfly.yaml)"


_VARIANT_HINT = _yaml_hint("variant names")


def _non_text_names(names: Iterable[Any], what: str) -> str | None:
    """The reason when some of *names* (``what``: ``metadata keys``...) are not text; ``None`` when all are."""
    bad = [name for name in names if not isinstance(name, str)]
    if not bad:
        return None
    return f"{what} must be strings{_yaml_hint(what) if any(isinstance(name, bool) for name in bad) else ''}"


class FlagDefinitionError(ValueError):
    """A definition, or a document, breaks a rule of the contract. ``key`` names the flag (or the document part)."""

    def __init__(self, key: str, reason: str) -> None:
        super().__init__(f"invalid feature flag {key!r}: {reason}")
        self.key = key
        self.reason = reason


@dataclass(frozen=True)
class FlagDocument:
    """A validated flagd document: every definition plain flagd (the shorthand already expanded)."""

    flags: dict[str, dict[str, Any]] = field(default_factory=dict)
    evaluators: dict[str, dict[str, Any]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_flagd(self) -> dict[str, Any]:
        """The document as flagd JSON (a deep copy; empty ``$evaluators``/``metadata`` left out)."""
        document: dict[str, Any] = {"flags": copy.deepcopy(self.flags)}
        if self.evaluators:
            document["$evaluators"] = copy.deepcopy(self.evaluators)
        if self.metadata:
            document["metadata"] = copy.deepcopy(self.metadata)
        return document


def utc_today() -> dt.date:
    """Today in UTC: the day expiry is computed against (spec 4.1)."""
    return dt.datetime.now(dt.UTC).date()


def is_valid_key(key: object) -> bool:
    """Whether *key* is a flag key (spec 4.1)."""
    return isinstance(key, str) and FLAG_KEY_PATTERN.fullmatch(key) is not None


def _text_key(key: Any) -> Any:
    """YAML reads ``123:`` as an int: a flag key is its text. Booleans stay booleans (and fail the key rule)."""
    return str(key) if isinstance(key, int) and not isinstance(key, bool) else key


def normalize_flags(flags: Mapping[Any, Any]) -> dict[Any, Any]:
    """Expand the shorthand (spec 4.2): ``true``/``false``/``"name"`` become flagd definitions.

    Every other value is deep-copied unchanged, for validation to judge.
    """
    normalized: dict[Any, Any] = {}
    for raw_key, value in flags.items():
        expanded = _expanded(value)
        normalized[_text_key(raw_key)] = copy.deepcopy(value) if expanded is value else expanded
    return normalized


def _expanded(value: Any) -> Any:
    """One flag's value with the shorthand expanded: a new definition, or *value* itself when it is none."""
    if isinstance(value, bool):
        return {"state": "ENABLED", "variants": {"on": True, "off": False}, "defaultVariant": "on" if value else "off"}
    if isinstance(value, str):
        return {"state": "ENABLED", "variants": {value: value}, "defaultVariant": value}
    return value


def _jsonable(value: Any) -> Any:
    """A copy of *value* (level 1) that gives YAML's ``date``/``datetime`` values their ISO text and int keys their
    text, at any depth, in the source's order.

    Walks with a stack of its own and rebuilds every container (a tuple becomes a list); scalars are shared. A *value*
    nesting deeper than :data:`MAX_DEFINITION_DEPTH`, or containing itself (YAML anchors can build that), comes back
    as it is, at the first container too deep: the depth rule refuses it, so converting it would be wasted work and,
    for a cycle, never end.
    """
    root: list[Any] = [None]
    pending: list[tuple[Any, Any, Any, int]] = [(value, root, 0, 1)]
    while pending:
        item, parent, slot, level = pending.pop()
        if not isinstance(item, Mapping | list | tuple):
            # datetime is a date subclass: both become isoformat() text
            parent[slot] = item.isoformat() if isinstance(item, dt.date) else item
            continue
        if level > MAX_DEFINITION_DEPTH:
            return value
        if isinstance(item, Mapping):
            members = [(_text_key(key), member) for key, member in item.items()]
            copied: Any = dict.fromkeys(key for key, _ in members)  # claims every slot, in the source's order
            # reversed: the last of two keys that read the same (1 and "1") is the one that stays
            pending.extend((member, copied, key, level + 1) for key, member in reversed(members))
        else:
            copied = [None] * len(item)
            pending.extend((member, copied, index, level + 1) for index, member in enumerate(item))
        parent[slot] = copied
    return root[0]


def _value_class(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, dict | list):
        return "object"
    return "null" if value is None else type(value).__name__


def flag_type(definition: Mapping[str, Any]) -> str:
    """``boolean``, ``string``, ``number`` or ``object``: the type of a valid definition's variant values."""
    variants = definition.get("variants") or {}
    kinds = {_value_class(value) for value in variants.values()}
    kind = kinds.pop() if len(kinds) == 1 else "object"
    return kind if kind in ("boolean", "string", "number") else "object"


def _is_empty_list(value: Any) -> bool:
    """Whether *value* is ``[]``, which stands for ``{}`` where the contract expects an object."""
    return isinstance(value, list) and not value


def _is_date(value: Any) -> bool:
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _scalars(values: Mapping[str, Any]) -> bool:
    return all(isinstance(value, str | int | float) for value in values.values())  # bool is an int


def _walk_reason(value: Any) -> str | None:
    """Why *value* (a flag definition, an evaluator rule or the document ``metadata``: level 1) breaks the two rules
    that reach its every corner, or ``None``.

    ``definition nests too deeply`` when an object or an array sits deeper than :data:`MAX_DEFINITION_DEPTH` (reported
    first, whatever else the walk met); otherwise ``numbers must be finite`` when a NaN or an infinity stands anywhere
    (YAML reads ``.nan``/``.inf``, JSON has neither). Walks with a stack of its own and ends at the first container too
    deep, so the work is bounded whatever the depth, and a structure that contains itself is refused, never looped on.
    """
    non_finite = False
    pending = [(value, 1)]
    while pending:
        item, level = pending.pop()
        if isinstance(item, Mapping):
            members: Iterable[Any] = item.values()
        elif isinstance(item, list | tuple):
            members = item
        else:
            non_finite = non_finite or (isinstance(item, float) and not math.isfinite(item))
            continue
        if level > MAX_DEFINITION_DEPTH:
            return "definition nests too deeply"
        pending.extend((member, level + 1) for member in members)
    return "numbers must be finite" if non_finite else None


def _metadata_key_reason(metadata: Mapping[Any, Any]) -> str | None:
    """The reason when a key of *metadata* is not text or is empty (FlagdCore refuses ``""``); ``None`` when valid."""
    if (reason := _non_text_names(metadata, "metadata keys")) is not None:
        return reason
    return "metadata keys must not be empty" if "" in metadata else None


def validate_flag(key: Any, definition: Any) -> None:
    """Raise :class:`FlagDefinitionError` with the contract message of the first rule *definition* breaks.

    The order is the contract's: key, object, state, variants, variant types, defaultVariant, targeting, metadata
    scalars, then the reserved metadata keys, then depth (``definition nests too deeply``: the definition is level 1 and
    every object or array inside adds one, at most :data:`MAX_DEFINITION_DEPTH`), then finite numbers. Depth is checked
    iteratively and before the numbers, so a definition too deep to walk is refused without inspecting them, and
    nothing here recurses whatever the definition's depth. An empty list stands for an empty object in
    ``targeting`` and ``metadata`` (a non-empty one is refused); :func:`parse_document` stores it as ``{}``. A ``null``
    variant value is no flag value (``variants must share one type``), alone or beside others. The names of the
    metadata entries must be non-empty text (``metadata keys must be strings``, where an unquoted YAML ``on:`` is a
    boolean key and gets the YAML hint; ``metadata keys must not be empty``). Fields beside ``state``, ``variants``,
    ``defaultVariant``, ``targeting`` and ``metadata`` are ignored (the contract's "unknown fields" rule), never an
    error, but a nesting too deep or a NaN or an infinity anywhere in the definition, those fields included, is
    (``numbers must be finite``: JSON cannot carry it, so the definition could be neither stored, served nor read by
    the other framework).
    """
    if not is_valid_key(key):
        raise FlagDefinitionError(str(key), "invalid flag key")
    if not isinstance(definition, Mapping):
        raise FlagDefinitionError(key, "flag definition must be an object")
    if definition.get("state") not in ("ENABLED", "DISABLED"):
        raise FlagDefinitionError(key, "state must be ENABLED or DISABLED")
    variants = definition.get("variants")
    if not isinstance(variants, Mapping) or not variants:
        raise FlagDefinitionError(key, "variants must be a non-empty object")
    if not all(isinstance(name, str) for name in variants):
        raise FlagDefinitionError(key, f"variants must be a non-empty object with text names{_VARIANT_HINT}")
    classes = {_value_class(value) for value in variants.values()}
    if len(classes) != 1 or "null" in classes:  # one JSON type, and null is not a flag value
        raise FlagDefinitionError(key, "variants must share one type")
    default_variant = definition.get("defaultVariant")
    if isinstance(default_variant, bool):
        raise FlagDefinitionError(key, f"defaultVariant is not a variant{_VARIANT_HINT}")
    if default_variant is not None and (not isinstance(default_variant, str) or default_variant not in variants):
        raise FlagDefinitionError(key, "defaultVariant is not a variant")
    targeting = definition.get("targeting")
    if targeting is not None and not isinstance(targeting, Mapping) and not _is_empty_list(targeting):
        raise FlagDefinitionError(key, "targeting must be an object")
    metadata = definition.get("metadata")
    if metadata is not None:
        _validate_flag_metadata(key, metadata)
    if (reason := _walk_reason(definition)) is not None:
        raise FlagDefinitionError(key, reason)


def _validate_flag_metadata(key: str, metadata: Any) -> None:
    if _is_empty_list(metadata):
        return
    if not isinstance(metadata, Mapping) or not _scalars(metadata):
        raise FlagDefinitionError(key, "metadata values must be scalars")
    if (reason := _metadata_key_reason(metadata)) is not None:
        raise FlagDefinitionError(key, reason)
    if "kind" in metadata and metadata["kind"] not in FLAG_KINDS:
        raise FlagDefinitionError(key, "kind must be one of release, experiment, ops, permission")
    if "expires" in metadata and not _is_date(metadata["expires"]):
        raise FlagDefinitionError(key, "expires must be a YYYY-MM-DD date")
    if "owner" in metadata and not isinstance(metadata["owner"], str):
        raise FlagDefinitionError(key, "owner must be a string")
    if "description" in metadata and not isinstance(metadata["description"], str):
        raise FlagDefinitionError(key, "description must be a string")


def _canonical(definition: dict[str, Any]) -> dict[str, Any]:
    """*definition*, a valid copy of its own, with an empty-list ``targeting`` or ``metadata`` stored as ``{}``."""
    for name in ("targeting", "metadata"):
        if _is_empty_list(definition.get(name)):
            definition[name] = {}
    return definition


def _section(raw: Mapping[Any, Any], name: str, reason: str) -> Mapping[Any, Any]:
    """The document section *name*: empty when absent, null or an empty list, otherwise it must be an object (even
    when falsy)."""
    value = raw.get(name)
    if value is None or _is_empty_list(value):
        return {}
    if not isinstance(value, Mapping):
        raise FlagDefinitionError(name, reason)
    return value


def parse_document(raw: Any, *, shorthand: bool = False) -> FlagDocument:
    """Validate a flagd document (``flags``, ``$evaluators``, ``metadata``) as a whole.

    With *shorthand* (configuration and test overrides only) the ``flags`` may use the spec 4.2 shorthand. YAML
    dates become their ISO text first. The first broken rule rejects the whole document. A ``$ref`` naming no
    evaluator is accepted: evaluating that flag yields ``PARSE_ERROR``.

    Each of the three sections is empty when absent, null or an empty list (``[]`` stands for ``{}``, as does a flag's
    ``targeting: []`` or ``metadata: []``, stored as ``{}``); a present section that is not an object is rejected
    whatever its value (``flags: false`` and ``flags: [x]`` are errors, not an empty set). The error's ``key`` is the
    section name (``flags``, ``$evaluators``, ``metadata``) and its reason ``<section> must be an object``. A document
    that is not an object at all is reported with the key ``<document>`` and ``document must be an object``. A single
    flag entry that is not an object keeps the contract phrase ``flag definition must be an object``, with the flag's
    key.

    The names of the evaluators and the keys of the document ``metadata`` must be text, as in a flag's ``metadata``:
    YAML reads an unquoted ``on:`` as a boolean key, which is refused rather than renamed. A document ``metadata`` key
    must not be empty either. A NaN or an infinity in an evaluator is refused with the key ``$evaluators`` (the
    reason names the evaluator), in the document ``metadata`` with the key ``metadata``.

    A definition nesting deeper than :data:`MAX_DEFINITION_DEPTH` levels is refused with ``definition nests too deeply``
    (the flag's key; ``$evaluators``, the reason naming the evaluator, as for a rule that is not an object or a number
    that is not finite), before any number is inspected. The document ``metadata`` takes the same walk after its own
    rules, so a nested value there is reported as no scalar. Nothing here recurses: a document nested thousands of
    levels deep, or one that contains itself, is refused as cheaply as a shallow one, never with a ``RecursionError``,
    and everything that is stored is a copy of its own.
    """
    if not isinstance(raw, Mapping):
        raise FlagDefinitionError("<document>", "document must be an object")
    flags_in = _section(raw, "flags", "flags must be an object")
    flags: dict[str, dict[str, Any]] = {}
    for raw_key, value in flags_in.items():
        key = _text_key(raw_key)
        definition = _jsonable(value)
        if shorthand:
            definition = _expanded(definition)
        validate_flag(key, definition)
        flags[key] = _canonical(definition)
    evaluators_in = {
        _text_key(name): rule for name, rule in _section(raw, "$evaluators", "$evaluators must be an object").items()
    }
    if (reason := _non_text_names(evaluators_in, "evaluator names")) is not None:
        raise FlagDefinitionError("$evaluators", reason)
    evaluators: dict[str, dict[str, Any]] = {}
    for name, rule in evaluators_in.items():
        if not isinstance(rule, Mapping):
            raise FlagDefinitionError("$evaluators", f"targeting must be an object (evaluator {name!r})")
        evaluator = _jsonable(rule)
        if (reason := _walk_reason(evaluator)) is not None:
            raise FlagDefinitionError("$evaluators", f"{reason} (evaluator {name!r})")
        evaluators[name] = evaluator
    metadata = _jsonable(_section(raw, "metadata", "metadata must be an object"))
    if not _scalars(metadata):
        raise FlagDefinitionError("metadata", "metadata values must be scalars")
    if (reason := _metadata_key_reason(metadata)) is not None:
        raise FlagDefinitionError("metadata", reason)
    if (reason := _walk_reason(metadata)) is not None:
        raise FlagDefinitionError("metadata", reason)
    return FlagDocument(flags=flags, evaluators=evaluators, metadata=metadata)


def is_expired(definition: Mapping[str, Any], today: dt.date) -> bool:
    """Whether ``metadata.expires`` is before *today*. An expired flag still evaluates normally."""
    metadata = definition.get("metadata")
    expires = metadata.get("expires") if isinstance(metadata, Mapping) else None
    return isinstance(expires, str) and _is_date(expires) and dt.date.fromisoformat(expires) < today


def expired_keys(flags: Mapping[str, Mapping[str, Any]], today: dt.date) -> list[str]:
    """The keys of the expired flags of *flags*, sorted."""
    return sorted(key for key, definition in flags.items() if is_expired(definition, today))
