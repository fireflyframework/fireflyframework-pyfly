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
"""Domain-driven design (DDD) building blocks.

Pure-Python primitives for modelling rich domain layers:

* :class:`Entity` — identity-based equality
* :class:`ValueObject` — structural equality, immutability marker
* :class:`AggregateRoot` — entity that owns a consistency boundary and
  collects pending :class:`DomainEvent` instances
* :class:`DomainEvent` — something that happened in the domain
* :class:`Specification` — composable in-memory predicate (``&``, ``|``, ``~``)
* :class:`DomainRepository` — collection-like protocol for aggregate stores
* :class:`DomainException` / :class:`BusinessRuleViolation` /
  :class:`AggregateNotFound` — domain-layer error types

These primitives mirror ``org.fireflyframework.starter.domain``
(Java) and ``FireflyFramework.Starter.Domain`` (.NET) and have zero
runtime dependencies — they are pure standard-library Python and can be
imported from any layer of the application.
"""

from __future__ import annotations

from pyfly.domain.aggregate_root import AggregateRoot
from pyfly.domain.domain_event import DomainEvent
from pyfly.domain.entity import Entity
from pyfly.domain.exceptions import (
    AggregateNotFound,
    BusinessRuleViolation,
    DomainException,
)
from pyfly.domain.repository import DomainRepository
from pyfly.domain.specification import Specification
from pyfly.domain.value_object import ValueObject

__all__ = [
    "AggregateNotFound",
    "AggregateRoot",
    "BusinessRuleViolation",
    "DomainEvent",
    "DomainException",
    "DomainRepository",
    "Entity",
    "Specification",
    "ValueObject",
]
