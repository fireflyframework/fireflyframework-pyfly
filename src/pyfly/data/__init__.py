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
"""PyFly Data Commons — shared abstractions for all data adapters.

PyFly Data is the umbrella module (like Spring Data Commons). It provides
shared abstractions — RepositoryPort[T, ID], QueryMethodParser,
QueryMethodCompilerPort, Page, Pageable — that all data adapters build on.

Adapters:
    - **PyFly Data Relational** (``pyfly.data.relational``) — SQLAlchemy async ORM.
    - **PyFly Data Document** (``pyfly.data.document``) — MongoDB via Beanie ODM.

This module exports ONLY framework-agnostic commons. Import backend-specific
types from ``pyfly.data.relational`` or ``pyfly.data.document`` directly.
"""

from pyfly.data.filter import BaseFilterUtils
from pyfly.data.mapper import Mapper, default_mapper, mapping
from pyfly.data.page import Page
from pyfly.data.pageable import Order, Pageable, Sort
from pyfly.data.ports.compiler import QueryMethodCompilerPort
from pyfly.data.ports.outbound import CrudRepository, PagingRepository, RepositoryPort, SessionPort
from pyfly.data.post_processor import DERIVED_PREFIXES, BaseRepositoryPostProcessor
from pyfly.data.projection import is_projection, projection, projection_fields
from pyfly.data.query import query
from pyfly.data.query_parser import QueryMethodParser
from pyfly.data.specification import Specification
from pyfly.data.transactional import Isolation, Propagation, transactional

__all__ = [
    "BaseFilterUtils",
    "BaseRepositoryPostProcessor",
    "CrudRepository",
    "DERIVED_PREFIXES",
    "Isolation",
    "Mapper",
    "Order",
    "Page",
    "Pageable",
    "PagingRepository",
    "Propagation",
    "QueryMethodCompilerPort",
    "QueryMethodParser",
    "RepositoryPort",
    "SessionPort",
    "Sort",
    "Specification",
    "default_mapper",
    "is_projection",
    "mapping",
    "projection",
    "projection_fields",
    "query",
    "transactional",
]
