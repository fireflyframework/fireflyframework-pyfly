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
"""BeanPostProcessor — hooks into bean creation lifecycle."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class BeanPostProcessor(Protocol):
    """Hook into bean initialization.

    Implementations are called for every bean created by the ApplicationContext:
    - ``before_init``: called before @post_construct
    - ``after_init``: called after @post_construct
    """

    def before_init(self, bean: Any, bean_name: str) -> Any:
        """Called before @post_construct. May return a replacement bean."""
        ...

    def after_init(self, bean: Any, bean_name: str) -> Any:
        """Called after @post_construct. May return a replacement bean."""
        ...
