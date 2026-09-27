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
"""Lifecycle annotations: @post_construct and @pre_destroy."""

from __future__ import annotations

import functools
import inspect
import weakref
from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

# The names of each class's methods marked with a lifecycle marker (``__pyfly_post_construct__``,
# ``__pyfly_pre_destroy__``), found once per class. Every bean the container creates is scanned, so
# a transient bean resolved per call must not pay a full ``dir()`` walk each time.
_MARKED_NAMES: weakref.WeakKeyDictionary[type, dict[str, tuple[str, ...]]] = weakref.WeakKeyDictionary()


def post_construct(func: F) -> F:
    """Mark a method to be called after the bean is fully initialized.

    Replaces the magic ``on_init`` method name convention.
    """
    func.__pyfly_post_construct__ = True  # type: ignore[attr-defined]
    return func


def pre_destroy(func: F) -> F:
    """Mark a method to be called before the bean is destroyed.

    Replaces the magic ``on_destroy`` method name convention.
    """
    func.__pyfly_pre_destroy__ = True  # type: ignore[attr-defined]
    return func


def marked_method_names(cls: type, marker: str) -> tuple[str, ...]:
    """The names of the methods of *cls* whose function carries *marker*, in ``dir()`` order.

    *marker* is the attribute a lifecycle decorator sets (``"__pyfly_pre_destroy__"``,
    ``"__pyfly_post_construct__"``). A property is never evaluated, a static or class method is
    looked into, and the answer is cached per class.
    """
    try:
        per_class = _MARKED_NAMES.setdefault(cls, {})
    except TypeError:  # a class that cannot be weakly referenced is scanned every time
        per_class = {}
    names = per_class.get(marker)
    if names is None:
        found: list[str] = []
        for attr_name in dir(cls):
            static_attr = inspect.getattr_static(cls, attr_name, None)
            if isinstance(static_attr, (property, functools.cached_property)):
                continue
            if isinstance(static_attr, (staticmethod, classmethod)):
                static_attr = static_attr.__func__
            if getattr(static_attr, marker, False):
                found.append(attr_name)
        names = per_class[marker] = tuple(found)
    return names
