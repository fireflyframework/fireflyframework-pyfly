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

The compiled query runs in the repository's unit of work: the unit's ``ClientSession`` goes with the ``find``
or the ``aggregate``. A find filter and a pipeline without an ``$out`` or ``$merge`` stage are reads (outside a
transaction they run without one). A pipeline with an ``$out`` or ``$merge`` stage writes, in one command:
outside a transaction it runs without one, and inside one it raises
:class:`~pyfly.data.transaction.errors.IllegalTransactionStateError` before it is sent, because MongoDB cannot run
either stage in a multi-document transaction.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar, cast

from pyfly.data.document.mongodb.transaction_manager import in_transaction
from pyfly.data.transaction.errors import IllegalTransactionStateError

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


class MongoAnnotatedQuery:
    """A compiled ``@query`` method: a find filter or an aggregation pipeline, with its placeholders.

    :meth:`run` executes it on a repository, in the repository's unit of work. Calling the object itself with
    a document class and the keyword arguments (``await query(Model, **kwargs)``, what the compiled callables of
    earlier releases took) runs it through a repository of that class.

    A pipeline with an ``$out`` or ``$merge`` stage is a write of one command (:attr:`reads` is ``False``): a
    repository operation built on it runs without a transaction outside one (``single=True``), and :meth:`run`
    refuses it inside a transaction, where MongoDB rejects both stages.
    """

    def __init__(self, template: Any) -> None:
        self.template = template
        self.is_pipeline = isinstance(template, list)
        writes = self.is_pipeline and any(
            isinstance(stage, dict) and ("$out" in stage or "$merge" in stage) for stage in template
        )
        self.reads = not writes
        """Whether the query only reads (a pipeline ending in ``$out`` or ``$merge`` writes)."""

    async def run(self, repository: Any, **kwargs: Any) -> Any:
        """Run the query on *repository* (a ``MongoRepository``): the matching documents of a find filter, or
        the rows (``dict``) of a pipeline.

        Raises:
            IllegalTransactionStateError: The query is a pipeline with an ``$out`` or ``$merge`` stage and the
                call's unit runs a multi-document transaction (nothing is sent, and the transaction stays
                usable).
        """
        document = _substitute_params(self.template, kwargs)
        if self.is_pipeline:
            if not self.reads:
                self._check_outside_transaction(repository)
            async with repository._operation(write=not self.reads) as session:
                cursor = await repository._collection().aggregate(document, session=session)
                return list(await cursor.to_list(length=None))
        return list(await repository._find(document))

    def _check_outside_transaction(self, repository: Any) -> None:
        """Refuse a pipeline that writes in a unit that runs a transaction: MongoDB rejects ``$out`` and
        ``$merge`` there (``OperationNotSupportedInTransaction``) and aborts the whole transaction."""
        unit = repository._current_unit()
        if in_transaction(unit):
            raise IllegalTransactionStateError(
                f"A pipeline with an $out or $merge stage cannot run in {unit.describe()}: MongoDB runs neither "
                "stage inside a multi-document transaction. Call it outside @transactional (a repository call "
                "outside a transaction runs the pipeline as one command, without one), or in a boundary with "
                "Propagation.NOT_SUPPORTED.",
                datasource=unit.datasource,
            )

    async def __call__(self, target: Any, **kwargs: Any) -> Any:
        """Run the query on *target*: a ``MongoRepository``, or a document class (through a repository of it)."""
        from pyfly.data.document.mongodb.repository import MongoRepository, repository_operation

        repository = target if isinstance(target, MongoRepository) else MongoRepository(target)

        async def execute(self_arg: Any) -> Any:
            return await self.run(self_arg, **kwargs)

        # A pipeline that writes is one command: outside a transaction it runs without one (single=True).
        return await repository_operation(execute, read=self.reads, atomic=True, single=not self.reads)(repository)


class MongoQueryExecutor:
    """Compile ``@query``-decorated methods into :class:`MongoAnnotatedQuery` objects.

    This class is used by :class:`MongoRepositoryBeanPostProcessor` to wire up
    custom query methods at startup time.
    """

    def compile_query_method(
        self,
        method: Callable[..., Any],
        entity: type[T],
    ) -> MongoAnnotatedQuery:
        """Compile a ``@query``-decorated method into an executable query.

        Args:
            method: The decorated method (must have ``__pyfly_query__``).
            entity: The Beanie document type.

        Returns:
            A :class:`MongoAnnotatedQuery` that returns ``list[entity]`` for a find filter (a JSON object) and
            ``list[dict]`` for an aggregation pipeline (a JSON array).

        Raises:
            AttributeError: If *method* was not decorated with ``@query``.
            ValueError: If the query string is not valid JSON.
        """
        if not hasattr(method, "__pyfly_query__"):
            raise AttributeError(f"{method} is not decorated with @query (missing __pyfly_query__)")

        query_string: str = method.__pyfly_query__
        return MongoAnnotatedQuery(json.loads(query_string.strip()))

    def _compile_find(self, query_string: str) -> Callable[..., Coroutine[Any, Any, Any]]:
        """A find filter as a query (see :class:`MongoAnnotatedQuery`)."""
        return cast(Callable[..., Coroutine[Any, Any, Any]], MongoAnnotatedQuery(json.loads(query_string)))

    def _compile_aggregate(self, query_string: str) -> Callable[..., Coroutine[Any, Any, Any]]:
        """An aggregation pipeline as a query (see :class:`MongoAnnotatedQuery`)."""
        return cast(Callable[..., Coroutine[Any, Any, Any]], MongoAnnotatedQuery(json.loads(query_string)))
