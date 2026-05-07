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
"""Concrete persistence adapters for the orchestration engine."""

from __future__ import annotations

from pyfly.transactional.persistence.cache_adapter import CachePersistenceProvider
from pyfly.transactional.persistence.redis_adapter import RedisPersistenceProvider
from pyfly.transactional.persistence.sqlalchemy_adapter import SqlAlchemyPersistenceProvider

__all__ = [
    "CachePersistenceProvider",
    "RedisPersistenceProvider",
    "SqlAlchemyPersistenceProvider",
]
