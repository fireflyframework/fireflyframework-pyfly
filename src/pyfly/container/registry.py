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
"""Service registration metadata."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pyfly.container.types import Scope


@dataclass
class Registration:
    """Metadata for a registered service."""

    impl_type: type
    scope: Scope = Scope.SINGLETON
    condition: Callable[..., bool] | None = None
    instance: Any = field(default=None, repr=False)
    name: str = ""
    # Optional factory (e.g. a @bean method closure). When set it takes
    # precedence over ``impl_type.__init__`` so TRANSIENT @bean factory logic is
    # preserved on every resolution instead of being reconstructed via __init__.
    factory: Callable[[], Any] | None = field(default=None, repr=False)
