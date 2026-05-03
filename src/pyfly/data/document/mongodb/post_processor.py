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
"""BeanPostProcessor that wires query methods onto MongoRepository beans."""

from __future__ import annotations

import logging
from typing import Any

from pyfly.data.document.mongodb.query import MongoQueryExecutor
from pyfly.data.document.mongodb.query_compiler import MongoQueryMethodCompiler
from pyfly.data.document.mongodb.repository import MongoRepository
from pyfly.data.post_processor import BaseRepositoryPostProcessor

logger = logging.getLogger(__name__)


class MongoRepositoryBeanPostProcessor(BaseRepositoryPostProcessor):
    """Replaces stub methods on :class:`MongoRepository` subclasses with real query implementations.

    For each method decorated with ``@query``, compiles the annotated JSON
    filter or aggregation pipeline into an executable async callable.  For
    each derived query stub (methods starting with ``find_by_``,
    ``count_by_``, ``exists_by_``, or ``delete_by_``), parses the method
    name and compiles a corresponding MongoDB query.
    """

    def __init__(self) -> None:
        super().__init__()
        self._query_compiler = MongoQueryMethodCompiler()
        self._query_executor = MongoQueryExecutor()

    # ------------------------------------------------------------------
    # Hook implementations
    # ------------------------------------------------------------------

    def _get_repository_type(self) -> type:
        return MongoRepository

    def _compile_derived(self, parsed: Any, entity: Any, bean: Any, *, return_type: Any = None) -> Any:
        return self._query_compiler.compile(parsed, bean._model, return_type=return_type)

    def _wrap_derived_method(self, compiled_fn: Any) -> Any:
        """Wrap a derived-query-compiled function to inject ``bean._model``."""

        async def wrapper(self_arg: Any, *args: Any) -> Any:
            return await compiled_fn(self_arg._model, *args)

        return wrapper

    def _process_query_decorated(self, bean: Any, cls: type, attr_name: str, attr: Any, entity: Any) -> bool:
        """Compile and bind ``@query``-decorated methods on MongoDB repositories."""
        if hasattr(attr, "__pyfly_query__"):
            compiled_fn = self._query_executor.compile_query_method(attr, entity)
            wrapper = self._wrap_query_method(compiled_fn)
            setattr(bean, attr_name, wrapper.__get__(bean, cls))
            return True
        return False

    # ------------------------------------------------------------------
    # Wrapper factories
    # ------------------------------------------------------------------

    @staticmethod
    def _wrap_query_method(compiled_fn: Any) -> Any:
        """Wrap a ``@query``-compiled function to inject ``bean._model``."""

        async def wrapper(self_arg: Any, **kwargs: Any) -> Any:
            return await compiled_fn(self_arg._model, **kwargs)

        return wrapper
