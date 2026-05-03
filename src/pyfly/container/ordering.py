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
"""Bean initialization ordering — @order decorator and precedence constants."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T", bound=type)

HIGHEST_PRECEDENCE: int = -(2**31)
LOWEST_PRECEDENCE: int = 2**31 - 1


def order(value: int) -> Callable[[T], T]:
    """Set the initialization order for a bean class.

    Lower value = higher priority (initialized first).
    Default order for undecorated beans is 0.
    """

    def decorator(cls: T) -> T:
        cls.__pyfly_order__ = value  # type: ignore[attr-defined]
        return cls

    return decorator


def get_order(cls: type) -> int:
    """Get the order value for a class, defaulting to 0."""
    return getattr(cls, "__pyfly_order__", 0)
