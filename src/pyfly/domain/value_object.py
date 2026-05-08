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
"""DDD :class:`ValueObject` — structural equality, immutability.

A *value object* has no identity of its own; it is fully described by
the values of its attributes. Two value objects are equal if every
attribute is equal. They are immutable: once constructed, their state
cannot change. To "modify" a value object you build a new one.

Implementations should subclass :class:`ValueObject` and decorate
themselves with ``@dataclass(frozen=True)`` (or ``@dataclass(frozen=True,
slots=True)`` for performance). The dataclass machinery already provides
the structural ``__eq__`` and ``__hash__`` semantics we need; this base
class adds a uniform :meth:`replace` helper, an explicit immutability
guarantee, and a clear marker type for static checks.
"""

from __future__ import annotations

import dataclasses
from typing import Any, TypeVar

T = TypeVar("T", bound="ValueObject")


class ValueObject:
    """Marker base class for all DDD value objects.

    Subclasses must be decorated with ``@dataclass(frozen=True)``.
    """

    def replace(self: T, **changes: Any) -> T:
        """Return a new value object with the supplied fields replaced.

        Equivalent to :func:`dataclasses.replace`, exposed as a method so
        the call site reads as a domain operation rather than a stdlib
        utility::

            money = Money(amount=100, currency="EUR")
            doubled = money.replace(amount=200)
        """
        if not dataclasses.is_dataclass(self):
            raise TypeError(
                f"{type(self).__name__} must be decorated with @dataclass(frozen=True) to use ValueObject.replace()."
            )
        return dataclasses.replace(self, **changes)
