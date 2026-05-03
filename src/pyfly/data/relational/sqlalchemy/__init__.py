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
"""SQLAlchemy data access adapter — default RepositoryPort implementation."""

from pyfly.data.relational.sqlalchemy.auditing import AuditingEntityListener
from pyfly.data.relational.sqlalchemy.entity import Base, BaseEntity, SoftDeleteMixin, VersionedMixin
from pyfly.data.relational.sqlalchemy.filter import FilterOperator, FilterUtils
from pyfly.data.relational.sqlalchemy.post_processor import RepositoryBeanPostProcessor
from pyfly.data.relational.sqlalchemy.query import QueryExecutor, query
from pyfly.data.relational.sqlalchemy.query_compiler import QueryMethodCompiler
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.relational.sqlalchemy.soft_delete import SoftDeleteRepository
from pyfly.data.relational.sqlalchemy.specification import Specification
from pyfly.data.relational.sqlalchemy.transactional import (
    Isolation,
    Propagation,
    _active_session_var,
    reactive_transactional,
    transactional,
)

__all__ = [
    "AuditingEntityListener",
    "Base",
    "BaseEntity",
    "FilterOperator",
    "FilterUtils",
    "Isolation",
    "Propagation",
    "QueryExecutor",
    "QueryMethodCompiler",
    "Repository",
    "RepositoryBeanPostProcessor",
    "SoftDeleteMixin",
    "SoftDeleteRepository",
    "Specification",
    "VersionedMixin",
    "_active_session_var",
    "query",
    "reactive_transactional",
    "transactional",
]
