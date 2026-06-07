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
"""@lazy — mark a bean for lazy initialization (the Spring @Lazy equivalent)."""

from __future__ import annotations

from typing import TypeVar

T = TypeVar("T", bound=type)


def lazy(cls: T) -> T:
    """Mark a bean as lazy-initialized.

    A lazy bean is **not** eagerly created during application startup; it is
    constructed on first resolution instead. Useful for expensive beans that may
    never be used, or to avoid doing heavy work at boot.
    """
    cls.__pyfly_lazy__ = True  # type: ignore[attr-defined]
    return cls
