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
"""Base BeanPostProcessor for wiring query methods onto repository beans.

Provides the shared iteration loop, stub detection, and derived-query
prefix matching used by both the SQLAlchemy and MongoDB adapters.
Adapter-specific behaviour is supplied via abstract hook methods.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, get_type_hints

from pyfly.data.query_parser import QueryMethodParser

# Prefixes that indicate a derived query method.
DERIVED_PREFIXES = ("find_by_", "count_by_", "exists_by_", "delete_by_")


class BaseRepositoryPostProcessor(ABC):
    """Template base for repository bean post-processors.

    Subclasses implement the adapter-specific hooks while inheriting the
    shared iteration loop, stub detection, and ``before_init``.
    """

    def __init__(self) -> None:
        self._query_parser = QueryMethodParser()

    def before_init(self, bean: Any, bean_name: str) -> Any:
        return bean

    def after_init(self, bean: Any, bean_name: str) -> Any:
        repo_type = self._get_repository_type()
        if not isinstance(bean, repo_type):
            return bean

        entity = bean._model  # type: ignore[attr-defined]
        cls = type(bean)

        # Collect names defined on the base repository class so we never
        # replace them.
        base_names = set(dir(repo_type))

        for attr_name in list(vars(cls)):
            if attr_name.startswith("_"):
                continue

            attr = getattr(cls, attr_name, None)
            if attr is None or not callable(attr):
                continue

            # --- Adapter-specific decorated methods (e.g., @query) ---
            if self._process_query_decorated(bean, cls, attr_name, attr, entity):
                continue

            # --- Derived query methods ---
            if attr_name in base_names:
                continue

            if any(attr_name.startswith(prefix) for prefix in DERIVED_PREFIXES) and self._is_stub(attr):
                parsed = self._query_parser.parse(attr_name)
                hints = get_type_hints(attr)
                return_type = hints.get("return")
                compiled_fn = self._compile_derived(parsed, entity, bean, return_type=return_type)
                wrapper = self._wrap_derived_method(compiled_fn)
                setattr(bean, attr_name, wrapper.__get__(bean, cls))

        return bean

    # ------------------------------------------------------------------
    # Abstract hooks
    # ------------------------------------------------------------------

    @abstractmethod
    def _get_repository_type(self) -> type:
        """Return the base repository class this post-processor targets."""
        ...

    @abstractmethod
    def _compile_derived(self, parsed: Any, entity: Any, bean: Any, *, return_type: Any = None) -> Any:
        """Compile a parsed derived query method name into an executable callable."""
        ...

    @abstractmethod
    def _wrap_derived_method(self, compiled_fn: Any) -> Any:
        """Wrap a compiled derived-query function for binding onto the bean."""
        ...

    def _process_query_decorated(self, bean: Any, cls: type, attr_name: str, attr: Any, entity: Any) -> bool:
        """Process adapter-specific decorated methods (e.g., ``@query``).

        Return ``True`` if the attribute was handled, ``False`` otherwise.
        Default implementation does nothing — override in adapters that
        support decorator-based queries.
        """
        return False

    # ------------------------------------------------------------------
    # Stub detection
    # ------------------------------------------------------------------

    @staticmethod
    def _is_stub(method: Any) -> bool:
        """Return ``True`` if *method* appears to be a stub (body is ``...`` or ``pass``).

        A method is considered a stub when its code object contains no
        meaningful constants beyond ``None`` (which is the implicit return
        for ``pass`` bodies and ``...`` / ``Ellipsis`` stubs).
        """
        func = method
        # Unwrap staticmethod / classmethod / descriptors
        if isinstance(func, (staticmethod, classmethod)):
            func = func.__func__
        if hasattr(func, "__wrapped__"):
            func = func.__wrapped__

        code = getattr(func, "__code__", None)
        if code is None:
            return False

        # A stub function compiled from ``...`` or ``pass`` has
        # co_consts == (None,) — the implicit ``return None``.
        # Some versions of Python also include Ellipsis for ``...``.
        consts = set(code.co_consts)
        consts.discard(None)
        consts.discard(Ellipsis)

        # A docstring is stored as a code constant; on its own it does not make
        # the method a real implementation, so a documented ``...``/``pass`` body
        # must still count as a stub (otherwise documented derived-query methods
        # are silently left as no-ops instead of getting a generated query).
        doc = getattr(func, "__doc__", None)
        if doc is not None:
            consts.discard(doc)

        # If nothing meaningful remains in the constants and there is
        # very little bytecode, treat as a stub.
        return len(consts) == 0 and code.co_code is not None
