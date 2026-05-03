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

from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


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
