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
"""What a cache may store, and how values travel in and out of it.

Every backend gives the same value semantics: a cache holds a *copy* of what was put, and a hit is a
value of its own that the caller may change freely.

- The in-memory adapter stores a copy (:func:`encode_copy`/:func:`decode_copy`): pickled bytes, or a
  deep copy for a value pickle cannot handle (an instance of a locally defined class).
- The JSON adapters (Redis, PostgreSQL) store :func:`cache_dumps` bytes. The encoder converts the common
  framework types (datetime, Decimal, UUID, set, bytes, dataclasses, Pydantic models) to a JSON-safe form, so a hit
  comes back as JSON types; the declarative decorators and the CQRS query bus rebuild the declared type
  with :func:`restore` (a Pydantic ``TypeAdapter``).

A live ORM object is never a cache value (:class:`CacheValueError`): a SQLAlchemy-mapped instance or a
Beanie document belongs to the unit of work that loaded it. Shared from a cache, one request would see
another's uncommitted edits, a rollback would leave every later hit broken (``DetachedInstanceError``),
and the next commit of whichever session holds it would write another request's changes. Cache a DTO
instead, and declare it as the return type: :func:`uncacheable_type` lets the decorators refuse an
entity return type when the method is decorated, before any call writes anything.
"""

from __future__ import annotations

import collections.abc
import copy
import dataclasses
import datetime
import decimal
import io
import json
import pickle
import sys
import types
import typing
import uuid
from typing import Any

_MAX_DEPTH = 64
"""How deep :func:`find_live_entity` looks into nested objects."""


class CacheValueError(TypeError):
    """A value the cache refuses to store: a live ORM object, or a value its backend cannot encode.

    It is a :class:`TypeError`, as the JSON encoder's refusal always was, so existing handlers keep working.
    """


# ---------------------------------------------------------------------------------------------------------
# Live ORM objects
# ---------------------------------------------------------------------------------------------------------


def live_entity_kind(value: Any) -> str | None:
    """What makes *value* itself a live ORM object (``None`` when it is not one); its contents are not
    inspected, see :func:`find_live_entity`."""
    if isinstance(value, type):
        return None
    cls = type(value)
    if getattr(cls, "_sa_class_manager", None) is not None:
        return f"a SQLAlchemy-mapped {cls.__qualname__} instance"
    beanie = sys.modules.get("beanie")
    if beanie is not None:
        if isinstance(value, beanie.Document):
            return f"a Beanie {cls.__qualname__} document"
        if isinstance(value, (beanie.Link, beanie.BackLink)):
            return f"a Beanie {cls.__qualname__} (a lazy reference to a document)"
    return None


_IMMUTABLE = (str, bytes, int, float, complex, bool, type(None), decimal.Decimal, uuid.UUID)
"""Values that need no copy."""

_ATOMS = (*_IMMUTABLE, bytearray, datetime.date, datetime.time, datetime.timedelta)
"""Values with nothing inside them to inspect."""


def find_live_entity(value: Any) -> str | None:
    """The first live ORM object found in *value* or anything it holds (containers, dataclasses, Pydantic
    models and plain objects), described; ``None`` when there is none."""
    return _find(value, set(), 0)


def _find(value: Any, seen: set[int], depth: int) -> str | None:
    if isinstance(value, _ATOMS):
        return None
    kind = live_entity_kind(value)
    if kind is not None or depth >= _MAX_DEPTH or id(value) in seen or isinstance(value, type):
        return kind
    seen.add(id(value))
    children: typing.Iterable[Any]
    if isinstance(value, dict):
        children = [*value.keys(), *value.values()]
    elif isinstance(value, (list, tuple, set, frozenset)):
        children = value
    else:
        children = [*getattr(value, "__dict__", {}).values(), *_slot_values(value)]
    for child in children:
        found = _find(child, seen, depth + 1)
        if found is not None:
            return found
    return None


def _slot_values(value: Any) -> list[Any]:
    values: list[Any] = []
    for cls in type(value).__mro__:
        for name in cls.__dict__.get("__slots__", ()):
            if hasattr(value, name) and name not in ("__dict__", "__weakref__"):
                values.append(getattr(value, name))
    return values


def _refuse(kind: str) -> CacheValueError:
    return CacheValueError(
        f"The cache refuses {kind}: a live ORM object belongs to the unit of work that loaded it, and a "
        "cached one would be shared across requests and outlive its session. Cache a DTO (a Pydantic model, "
        "a dataclass or a dict) built from it instead."
    )


def check_cacheable(value: Any) -> None:
    """Raise :class:`CacheValueError` when *value* is, or holds, a live ORM object."""
    kind = find_live_entity(value)
    if kind is not None:
        raise _refuse(kind)


def uncacheable_type(annotation: Any) -> str | None:
    """What makes the declared type *annotation* uncacheable (``None`` when nothing does): an ORM-mapped
    class or a Beanie document class, on its own or as an argument of a generic (``list[Order]``,
    ``Order | None``, ``Page[Order]``)."""
    return _uncacheable(annotation, 0)


def _uncacheable(annotation: Any, depth: int) -> str | None:
    if depth > _MAX_DEPTH:
        return None
    if isinstance(annotation, type) and not isinstance(annotation, types.GenericAlias):
        if getattr(annotation, "_sa_class_manager", None) is not None:
            return f"the SQLAlchemy-mapped class {annotation.__qualname__}"
        beanie = sys.modules.get("beanie")
        if beanie is not None and issubclass(annotation, beanie.Document):
            return f"the Beanie document class {annotation.__qualname__}"
    for argument in typing.get_args(annotation):
        found = _uncacheable(argument, depth + 1)
        if found is not None:
            return found
    return None


# ---------------------------------------------------------------------------------------------------------
# Copies (the in-memory adapter and transaction-deferred writes)
# ---------------------------------------------------------------------------------------------------------


class _CopyingPickler(pickle.Pickler):
    """Pickles a value and refuses every live ORM object it meets on the way."""

    def reducer_override(self, obj: Any) -> Any:
        kind = live_entity_kind(obj)
        if kind is not None:
            raise _refuse(kind)
        return NotImplemented


class Copy:
    """A detached copy of a cache value: pickled bytes, or a deep copy when the value cannot be pickled.

    The bytes are pickled by this process and unpickled by it, and never leave it: nothing untrusted is
    ever unpickled here (the JSON encoding is what crosses process boundaries).
    """

    __slots__ = ("_payload", "_pickled")

    def __init__(self, payload: Any, *, pickled: bool) -> None:
        self._payload = payload
        self._pickled = pickled

    def value(self) -> Any:
        """A new copy of the value, the caller's own."""
        if self._pickled:
            return pickle.loads(self._payload)  # noqa: S301 — bytes this process pickled itself
        return copy.deepcopy(self._payload)


def encode_copy(value: Any) -> Copy:
    """A detached copy of *value*; raises :class:`CacheValueError` when *value* is, or holds, a live ORM
    object, or can be neither pickled nor deep-copied."""
    buffer = io.BytesIO()
    try:
        _CopyingPickler(buffer, protocol=pickle.HIGHEST_PROTOCOL).dump(value)
    except CacheValueError:
        raise
    except Exception:  # noqa: BLE001 — not picklable (a local class, a lambda): try a deep copy instead
        check_cacheable(value)
        try:
            return Copy(copy.deepcopy(value), pickled=False)
        except Exception as error:  # noqa: BLE001
            raise CacheValueError(
                f"The cache cannot store a copy of a {type(value).__qualname__} value: it can be neither "
                f"pickled nor deep-copied ({type(error).__name__}: {error})."
            ) from error
    return Copy(buffer.getvalue(), pickled=True)


def copy_value(value: Any) -> Any:
    """A detached copy of *value* (see :func:`encode_copy`)."""
    if isinstance(value, _IMMUTABLE):
        return value
    return encode_copy(value).value()


# ---------------------------------------------------------------------------------------------------------
# JSON (the Redis and PostgreSQL adapters)
# ---------------------------------------------------------------------------------------------------------


def _default(obj: Any) -> Any:
    kind = live_entity_kind(obj)
    if kind is not None:
        raise _refuse(kind)
    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()
    if isinstance(obj, decimal.Decimal):
        return str(obj)
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    # A dataclass → a dict of its fields (encoded in turn), which restore() rebuilds.
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
    # Pydantic v2 model → JSON-mode dict, once nothing inside it is a live ORM object. Computed fields are
    # left out: they are derived from the stored fields, and a model with extra="forbid" would refuse them
    # when restore() rebuilds it.
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        check_cacheable(obj)
        return model_dump(mode="json", exclude_computed_fields=True)
    raise CacheValueError(f"Object of type {type(obj).__name__} is not cache-serializable")


def cache_dumps(value: Any) -> bytes:
    """Serialize *value* to JSON bytes, tolerating framework types.

    Raises :class:`CacheValueError` for a live ORM object and for a value JSON cannot represent.
    """
    try:
        return json.dumps(value, default=_default).encode("utf-8")
    except CacheValueError:
        raise
    except (TypeError, ValueError) as error:  # a circular reference, a non-string dict key
        raise CacheValueError(f"The value is not cache-serializable: {error}") from error


def cache_loads(raw: Any) -> Any:
    """Deserialize cached JSON bytes/str back to a Python object.

    Returns plain JSON types (dict/list/str/int/float/bool/None); :func:`restore` rebuilds a declared type.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


# ---------------------------------------------------------------------------------------------------------
# The declared type of a hit
# ---------------------------------------------------------------------------------------------------------

_ADAPTERS: dict[Any, Any] = {}


def _type_adapter(annotation: Any) -> Any:
    """The (cached) Pydantic ``TypeAdapter`` of *annotation*; ``None`` when Pydantic cannot build one."""
    try:
        return _ADAPTERS[annotation]
    except KeyError:
        pass
    except TypeError:  # an unhashable annotation: build one each time
        return _build_adapter(annotation)
    adapter = _build_adapter(annotation)
    _ADAPTERS[annotation] = adapter
    return adapter


def _build_adapter(annotation: Any) -> Any:
    from pydantic import TypeAdapter

    try:
        return TypeAdapter(annotation)
    except Exception:  # noqa: BLE001 — a type Pydantic has no schema for: hits are returned as stored
        return None


def restore(value: Any, annotation: Any) -> Any:
    """*value* (a hit) as the declared type *annotation*.

    A JSON backend returns JSON types; this rebuilds Pydantic models, dataclasses, datetimes, decimals and
    UUIDs from them (an in-memory hit is already of the declared type and comes back unchanged). Fields are
    accepted by name and by alias: the encoder writes a model's field names (its serialization aliases when
    the model is configured to serialize by alias), whatever validation aliases the model declares. With no
    usable annotation the value is returned as stored.

    The value is validated, so it is also coerced to the annotation: a function declared ``-> int`` that
    returned ``"42"`` gets ``42`` from a hit. Raises ``pydantic.ValidationError`` when the stored value does
    not fit the type: an entry written by an older version of the model, a function that returns something
    other than its declared type, or a model field declared ``Field(exclude=True)`` without a default (the
    encoder leaves it out).

    A structural type (a ``typing.Protocol``) cannot be rebuilt: the value is returned as stored. An
    ``Iterable[X]`` is rebuilt as the ``list[X]`` that was stored (Pydantic would return a one-shot lazy
    iterator).
    """
    if value is None or annotation is typing.Any or annotation is object or annotation in (None, type(None)):
        return value
    plain_class = isinstance(annotation, type) and not isinstance(annotation, types.GenericAlias)
    if plain_class:
        if getattr(annotation, "_is_protocol", False):
            return value  # a Protocol describes a shape, not a type a value can be rebuilt as
        try:
            if isinstance(value, annotation):
                return value  # already the declared type (an in-memory hit): nothing to rebuild
        except TypeError:  # a class whose instance check refuses to run: no fast path
            pass
    if typing.get_origin(annotation) is collections.abc.Iterable:
        annotation = list[typing.get_args(annotation) or (typing.Any,)]  # type: ignore[misc]
    adapter = _type_adapter(annotation)
    if adapter is None:
        return value
    return adapter.validate_python(value, by_alias=True, by_name=True)
