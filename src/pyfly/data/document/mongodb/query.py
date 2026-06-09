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
"""MongoDB ``@query`` executor — compiles decorated methods into Beanie operations.

Provides :class:`MongoQueryExecutor` which turns ``@query``-decorated methods
(carrying a JSON filter document or aggregation pipeline) into async callables
that execute against a Beanie document model.

The query string may be:

- A **find filter** (starts with ``{``):
  ``'{"email": ":email", "active": true}'``

- An **aggregation pipeline** (starts with ``[``):
  ``'[{"$match": {"status": ":status"}}, {"$group": {"_id": "$category"}}]'``

Named parameters use the ``:param_name`` convention inside JSON string values.
During execution, ``":param_name"`` is replaced with the actual keyword-argument
value while preserving the Python type (int, bool, list, etc.).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _substitute_params(obj: Any, params: dict[str, Any]) -> Any:
    """Recursively walk a parsed JSON structure and replace ``:param`` placeholders.

    Substitution rules:

    - A **string** value that is exactly ``":param_name"`` is replaced by the
      corresponding value from *params*, preserving the Python type.
    - A **string** value that *contains* ``:param_name`` among other text is
      treated as a string-interpolation: the ``:param_name`` portion is
      replaced with ``str(value)``.
    - Dicts and lists are recursed into.
    - All other types (int, float, bool, None) pass through unchanged.
    """
    if isinstance(obj, dict):
        return {key: _substitute_params(value, params) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_substitute_params(item, params) for item in obj]
    if isinstance(obj, str):
        # Exact match: the entire string is a single placeholder
        stripped = obj.strip()
        if stripped.startswith(":") and stripped[1:] in params:
            return params[stripped[1:]]

        # Partial / embedded placeholders within a larger string
        result: str = obj
        for param_name, param_value in params.items():
            placeholder = f":{param_name}"
            if placeholder in result:
                result = result.replace(placeholder, str(param_value))
        return result

    return obj


class MongoQueryExecutor:
    """Compile ``@query``-decorated methods into async callables for MongoDB.

    This class is used by :class:`MongoRepositoryBeanPostProcessor` to wire up
    custom query methods at startup time.
    """

    def compile_query_method(
        self,
        method: Callable[..., Any],
        entity: type[T],
    ) -> Callable[..., Coroutine[Any, Any, Any]]:
        """Compile a ``@query``-decorated method into an executable async function.

        Args:
            method: The decorated method (must have ``__pyfly_query__``).
            entity: The Beanie document type (used for ``.find()`` /
                    ``.aggregate()`` calls).

        Returns:
            An async function with signature
            ``(model: type[T], **kwargs) -> Any`` that returns:

            - ``list[entity]`` for find-filter queries (JSON object).
            - ``list[dict]`` for aggregation-pipeline queries (JSON array).

        Raises:
            AttributeError: If *method* was not decorated with ``@query``.
            ValueError: If the query string is not valid JSON.
        """
        if not hasattr(method, "__pyfly_query__"):
            raise AttributeError(f"{method} is not decorated with @query (missing __pyfly_query__)")

        query_string: str = method.__pyfly_query__
        stripped = query_string.strip()

        # Parse once at compile time to validate JSON and detect query type.
        parsed = json.loads(stripped)
        is_pipeline = isinstance(parsed, list)

        if is_pipeline:
            return self._compile_aggregate(stripped)
        return self._compile_find(stripped)

    def _compile_find(
        self,
        query_string: str,
    ) -> Callable[..., Coroutine[Any, Any, Any]]:
        """Build an async callable that executes a ``find`` with the given filter."""
        template = json.loads(query_string)

        async def _execute(model: type[T], **kwargs: Any) -> list[Any]:
            filter_doc = _substitute_params(template, kwargs)
            return list(await model.find(filter_doc).to_list())  # type: ignore[attr-defined]

        return _execute

    def _compile_aggregate(
        self,
        query_string: str,
    ) -> Callable[..., Coroutine[Any, Any, Any]]:
        """Build an async callable that executes an aggregation pipeline.

        Uses the underlying pymongo async collection directly (via
        ``get_pymongo_collection()``) instead of Beanie's
        ``.aggregate()`` wrapper, because aggregation pipelines return
        raw dicts (not document instances) and the driver-level API is
        more reliable across async mock drivers.
        """
        template = json.loads(query_string)

        async def _execute(model: type[T], **kwargs: Any) -> list[dict[str, Any]]:
            pipeline = _substitute_params(template, kwargs)
            collection = model.get_pymongo_collection()  # type: ignore[attr-defined]
            cursor = collection.aggregate(pipeline)
            return list(await cursor.to_list(length=None))

        return _execute
