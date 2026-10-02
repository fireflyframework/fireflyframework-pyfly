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
"""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import math
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "FLAG_KEY_PATTERN",
    "FLAG_KINDS",
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
    return {_text_key(raw_key): _normalized(value) for raw_key, value in flags.items()}


def _normalized(value: Any) -> Any:
    """One flag's value with the shorthand expanded, or a deep copy of it."""
    if isinstance(value, bool):
        return {"state": "ENABLED", "variants": {"on": True, "off": False}, "defaultVariant": "on" if value else "off"}
    if isinstance(value, str):
        return {"state": "ENABLED", "variants": {value: value}, "defaultVariant": value}
    return copy.deepcopy(value)


@contextlib.contextmanager
def _not_too_deep(key: str, hint: str = "") -> Iterator[None]:
    """Turn a ``RecursionError`` (copying a definition nested deeper than Python's stack allows) into the error of
    the definition at *key*: a source keeps its last good document, a store write answers ``invalid-definition``."""
    try:
        yield
    except RecursionError:
        raise FlagDefinitionError(key, f"definition nests too deeply{hint}") from None


def _jsonable(value: Any) -> Any:
    """Give YAML's ``date``/``datetime`` values back their ISO text, and int keys their text, at any depth."""
    if isinstance(value, dt.date):  # datetime is a date subclass: both become isoformat() text
        return value.isoformat()
    if isinstance(value, Mapping):
        return {_text_key(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value


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


def _non_finite(value: Any) -> bool:
    """Whether a NaN or an infinity stands anywhere in *value*: YAML reads ``.nan``/``.inf``, JSON has neither.

    Walks with a stack of its own, so a deeply nested definition cannot exhaust Python's.
    """
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, float):
            if not math.isfinite(item):
                return True
        elif isinstance(item, Mapping):
            pending.extend(item.values())
        elif isinstance(item, list | tuple):
            pending.extend(item)
    return False


def _metadata_key_reason(metadata: Mapping[Any, Any]) -> str | None:
    """The reason when a key of *metadata* is not text or is empty (FlagdCore refuses ``""``); ``None`` when valid."""
    if (reason := _non_text_names(metadata, "metadata keys")) is not None:
        return reason
    return "metadata keys must not be empty" if "" in metadata else None


def validate_flag(key: Any, definition: Any) -> None:
    """Raise :class:`FlagDefinitionError` with the contract message of the first rule *definition* breaks.

    The order is the contract's: key, object, state, variants, variant types, defaultVariant, targeting, metadata
    scalars, then the reserved metadata keys, then finite numbers. The names of the metadata entries must be
    non-empty text (``metadata keys must be strings``, where an unquoted YAML ``on:`` is a boolean key and gets the
    YAML hint; ``metadata keys must not be empty``). Fields beside ``state``, ``variants``, ``defaultVariant``,
    ``targeting`` and ``metadata`` are ignored (the contract's "unknown fields" rule), never an error, but a NaN or an
    infinity anywhere in the definition, those fields included, is (``numbers must be finite``): JSON cannot carry
    it, so the definition could be neither stored, served nor read by the other framework.
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
    if len({_value_class(value) for value in variants.values()}) != 1:
        raise FlagDefinitionError(key, "variants must share one type")
    default_variant = definition.get("defaultVariant")
    if isinstance(default_variant, bool):
        raise FlagDefinitionError(key, f"defaultVariant is not a variant{_VARIANT_HINT}")
    if default_variant is not None and (not isinstance(default_variant, str) or default_variant not in variants):
        raise FlagDefinitionError(key, "defaultVariant is not a variant")
    targeting = definition.get("targeting")
    if targeting is not None and not isinstance(targeting, Mapping):
        raise FlagDefinitionError(key, "targeting must be an object")
    metadata = definition.get("metadata")
    if metadata is not None:
        _validate_flag_metadata(key, metadata)
    if _non_finite(definition):
        raise FlagDefinitionError(key, "numbers must be finite")


def _validate_flag_metadata(key: str, metadata: Any) -> None:
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


def _section(raw: Mapping[Any, Any], name: str, reason: str) -> Mapping[Any, Any]:
    """The document section *name*: empty when absent or null, otherwise it must be an object (even when falsy)."""
    value = raw.get(name)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise FlagDefinitionError(name, reason)
    return value


def parse_document(raw: Any, *, shorthand: bool = False) -> FlagDocument:
    """Validate a flagd document (``flags``, ``$evaluators``, ``metadata``) as a whole.

    With *shorthand* (configuration and test overrides only) the ``flags`` may use the spec 4.2 shorthand. YAML
    dates become their ISO text first. The first broken rule rejects the whole document. A ``$ref`` naming no
    evaluator is accepted: evaluating that flag yields ``PARSE_ERROR``.

    Each of the three sections is empty when absent or null; a present section that is not an object is rejected
    whatever its value (``flags: false`` is an error, not an empty set). The error's ``key`` is the section name
    (``flags``, ``$evaluators``, ``metadata``) and its reason ``<section> must be an object``; ``<document>`` is the
    key when the document itself is not an object. A single flag entry that is not an object keeps the contract
    phrase ``flag definition must be an object``, with the flag's key.

    The names of the evaluators and the keys of the document ``metadata`` must be text, as in a flag's ``metadata``:
    YAML reads an unquoted ``on:`` as a boolean key, which is refused rather than renamed. A document ``metadata`` key
    must not be empty either. A NaN or an infinity in an evaluator is refused with the key ``$evaluators`` (the
    reason names the evaluator), in the document ``metadata`` with the key ``metadata``.

    A definition nested deeper than Python's stack lets it be copied is refused with ``definition nests too deeply``
    (the flag key; ``$evaluators``, the reason naming the evaluator; ``<document>`` when even reading the document
    runs out of stack), never a ``RecursionError``.
    """
    if not isinstance(raw, Mapping):
        raise FlagDefinitionError("<document>", "flag definition must be an object")
    with _not_too_deep("<document>"):
        raw = _jsonable(raw)
    flags_in = _section(raw, "flags", "flags must be an object")
    flags: dict[str, dict[str, Any]] = {}
    for raw_key, value in flags_in.items():
        key = _text_key(raw_key)
        with _not_too_deep(str(key)):
            definition = _normalized(value) if shorthand else value
            validate_flag(key, definition)
            flags[key] = copy.deepcopy(dict(definition))
    evaluators_in = _section(raw, "$evaluators", "$evaluators must be an object")
    if (reason := _non_text_names(evaluators_in, "evaluator names")) is not None:
        raise FlagDefinitionError("$evaluators", reason)
    evaluators: dict[str, dict[str, Any]] = {}
    for name, rule in evaluators_in.items():
        if not isinstance(rule, Mapping):
            raise FlagDefinitionError(f"$evaluators.{name}", "targeting must be an object")
        if _non_finite(rule):
            raise FlagDefinitionError("$evaluators", f"numbers must be finite (evaluator {name!r})")
        with _not_too_deep("$evaluators", f" (evaluator {name!r})"):
            evaluators[name] = copy.deepcopy(dict(rule))
    metadata = _section(raw, "metadata", "metadata must be an object")
    if not _scalars(metadata):
        raise FlagDefinitionError("metadata", "metadata values must be scalars")
    if (reason := _metadata_key_reason(metadata)) is not None:
        raise FlagDefinitionError("metadata", reason)
    if _non_finite(metadata):
        raise FlagDefinitionError("metadata", "numbers must be finite")
    return FlagDocument(flags=flags, evaluators=evaluators, metadata=dict(metadata))


def is_expired(definition: Mapping[str, Any], today: dt.date) -> bool:
    """Whether ``metadata.expires`` is before *today*. An expired flag still evaluates normally."""
    metadata = definition.get("metadata")
    expires = metadata.get("expires") if isinstance(metadata, Mapping) else None
    return isinstance(expires, str) and _is_date(expires) and dt.date.fromisoformat(expires) < today


def expired_keys(flags: Mapping[str, Mapping[str, Any]], today: dt.date) -> list[str]:
    """The keys of the expired flags of *flags*, sorted."""
    return sorted(key for key, definition in flags.items() if is_expired(definition, today))
