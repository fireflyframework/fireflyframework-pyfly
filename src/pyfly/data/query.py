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
"""Backend-neutral ``@query`` decorator for Spring Data-style custom queries.

Attaches query metadata (``__pyfly_query__`` and ``__pyfly_query_native__``)
to repository methods.  The actual compilation and execution is handled by
each data adapter's post-processor:

- **SQLAlchemy**: interprets the string as SQL / JPQL.
- **MongoDB**: interprets the string as a JSON filter document or aggregation pipeline.

Usage::

    from pyfly.data.query import query

    class UserRepository(Repository[User, UUID]):

        @query("SELECT u FROM User u WHERE u.email = :email")
        async def find_by_email(self, email: str) -> list[User]: ...

    class UserDocRepo(MongoRepository[UserDoc, str]):

        @query('{"email": ":email", "active": true}')
        async def find_active_by_email(self, email: str) -> list[UserDoc]: ...
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def query(value: str, *, native: bool = False) -> Callable[..., Any]:
    """Mark a repository method with a custom query.

    The query string uses named parameters (``:param_name``) that map to
    method keyword-argument names.

    Args:
        value: The query string.  Interpretation depends on the data adapter:

            - **SQLAlchemy** — SQL or JPQL-like syntax with ``:param`` placeholders.
            - **MongoDB** — JSON filter document (``{...}``) or aggregation
              pipeline (``[{...}, ...]``) with ``":param"`` placeholders.

        native: If ``True``, treat *value* as raw / native syntax (e.g. raw
                SQL for the relational adapter).  Defaults to ``False``.

    Returns:
        A decorator that stores query metadata on the wrapped function via
        ``__pyfly_query__`` and ``__pyfly_query_native__`` attributes.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        func.__pyfly_query__ = value  # type: ignore[attr-defined]
        func.__pyfly_query_native__ = native  # type: ignore[attr-defined]
        return func

    return decorator
