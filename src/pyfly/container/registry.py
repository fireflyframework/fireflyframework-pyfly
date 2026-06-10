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
from typing import Any, get_args

from pyfly.container.types import Scope, ScopeSpec


@dataclass
class Registration:
    """Metadata for a registered service."""

    impl_type: type
    scope: ScopeSpec = Scope.SINGLETON  # a built-in Scope or a custom scope name (str)
    condition: Callable[..., bool] | None = None
    instance: Any = field(default=None, repr=False)
    name: str = ""
    # Marks this the primary candidate among several beans of one interface —
    # used for @bean factories (class-level @primary uses __pyfly_primary__).
    primary: bool = False
    # Optional factory (e.g. a @bean method closure). When set it takes
    # precedence over ``impl_type.__init__`` so TRANSIENT @bean factory logic is
    # preserved on every resolution instead of being reconstructed via __init__.
    factory: Callable[[], Any] | None = field(default=None, repr=False)
    # Cached constructor injection plan — list of (param_name, resolved_type, has_default),
    # or None for a trivial ``object.__init__``. Computed once (lazily, on first create) so
    # the costly ``get_type_hints`` + ``inspect.signature`` are not re-run on every resolve.
    # ``init_plan_built`` distinguishes "not built yet" from "built, trivial (None)".
    init_plan: list[tuple[str, Any, bool]] | None = field(default=None, repr=False, compare=False)
    init_plan_built: bool = field(default=False, repr=False, compare=False)

    @property
    def display_name(self) -> str:
        """Readable bean name: the explicit ``name`` if set, otherwise a
        union-safe rendering of ``impl_type``.

        PEP 604 unions (``X | Y``) and other typing constructs have no
        ``__name__``, so deriving a default name via ``impl_type.__name__``
        raises ``AttributeError``. Fall back to the union member names, then
        ``str()``, so bean-name derivation never crashes on a union impl type.
        """
        if self.name:
            return self.name
        impl = self.impl_type
        direct = getattr(impl, "__name__", None)
        if isinstance(direct, str):
            return direct
        args = get_args(impl)
        if args:
            return " | ".join(getattr(a, "__name__", None) or str(a) for a in args)
        return str(impl)
