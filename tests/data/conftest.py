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
"""Test-environment compatibility shim for mongomock + beanie 2.x.

beanie 2.1's ``init_beanie`` calls
``database.list_collection_names(authorizedCollections=True, nameOnly=True)``.
``mongomock_motor`` proxies those kwargs straight through to mongomock's
``Database.list_collection_names(filter=None, session=None)`` — which raises
``TypeError`` on the server-only kwargs. We patch mongomock's method to accept
and ignore them so the in-memory Mongo tests run against beanie 2.x.

This is test-only and a no-op when mongomock is not installed.
"""

from __future__ import annotations

try:
    import mongomock.database as _mongomock_database
except ImportError:  # pragma: no cover - mongomock is an optional test dep
    _mongomock_database = None  # type: ignore[assignment]

if _mongomock_database is not None:
    _Database = _mongomock_database.Database
    _original_list_collection_names = _Database.list_collection_names

    if not getattr(_original_list_collection_names, "__pyfly_kwargs_shim__", False):

        def _list_collection_names(self, filter=None, session=None, **_ignored):  # noqa: A002
            """mongomock list_collection_names tolerant of beanie's extra kwargs."""
            return _original_list_collection_names(self, filter=filter, session=session)

        _list_collection_names.__pyfly_kwargs_shim__ = True  # type: ignore[attr-defined]
        _Database.list_collection_names = _list_collection_names  # type: ignore[method-assign]
