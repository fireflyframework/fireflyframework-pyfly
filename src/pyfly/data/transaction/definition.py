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
"""What a transactional boundary asks for: propagation, isolation, read-only, timeout, rules, datasource.

:class:`Propagation` and :class:`Isolation` are the enums ``@transactional`` has always exported
(``pyfly.data.Propagation`` is this class); :class:`TransactionDefinition` bundles one boundary's
settings for the :class:`~pyfly.data.transaction.template.TransactionTemplate`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from pyfly.data.transaction.rules import RollbackRules


class Propagation(enum.Enum):
    """How a boundary relates to the unit of work already bound for its datasource (Spring's semantics).

    ======================  ===========================================  ==========================
    Propagation             A unit is bound for the datasource           None is bound
    ======================  ===========================================  ==========================
    ``REQUIRED``            join it (participate)                        begin a new unit
    ``REQUIRES_NEW``        suspend it, begin a new unit, resume on exit begin a new unit
    ``NESTED``              savepoint inside it                          begin a new unit
    ``SUPPORTS``            join it                                      run without a unit
    ``NOT_SUPPORTED``       suspend it, run without a unit               run without a unit
    ``MANDATORY``           join it                                      ``IllegalTransactionStateError``
    ``NEVER``               ``IllegalTransactionStateError``             run without a unit
    ======================  ===========================================  ==========================

    "Without a unit" means repository calls get short auto units of their own.
    """

    REQUIRED = "REQUIRED"
    REQUIRES_NEW = "REQUIRES_NEW"
    NESTED = "NESTED"
    SUPPORTS = "SUPPORTS"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    NEVER = "NEVER"
    MANDATORY = "MANDATORY"


class Isolation(enum.Enum):
    """Transaction isolation level. ``DEFAULT`` keeps the database's (or the engine's) own level."""

    DEFAULT = "DEFAULT"
    READ_UNCOMMITTED = "READ UNCOMMITTED"
    READ_COMMITTED = "READ COMMITTED"
    REPEATABLE_READ = "REPEATABLE READ"
    SERIALIZABLE = "SERIALIZABLE"


@dataclass(frozen=True, slots=True)
class TransactionDefinition:
    """The settings of one transactional boundary.

    ``timeout`` is in seconds and applies to a new unit only (a participant cannot extend its unit's
    deadline). ``rollback_for`` / ``no_rollback_for`` are additive rollback rules
    (:mod:`pyfly.data.transaction.rules`). ``datasource`` names the datasource whose transaction manager
    runs the boundary; ``None`` lets the caller resolve it. ``name`` labels the unit in logs and errors.
    """

    propagation: Propagation = Propagation.REQUIRED
    isolation: Isolation = Isolation.DEFAULT
    read_only: bool = False
    timeout: float | None = None
    rollback_for: tuple[type[BaseException], ...] = ()
    no_rollback_for: tuple[type[BaseException], ...] = ()
    datasource: str | None = None
    name: str | None = None
    rules: RollbackRules = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError(f"timeout must be a positive number of seconds, got {self.timeout!r}")
        for label, types in (("rollback_for", self.rollback_for), ("no_rollback_for", self.no_rollback_for)):
            for candidate in types:
                if not (isinstance(candidate, type) and issubclass(candidate, BaseException)):
                    raise TypeError(f"{label} takes exception classes, got {candidate!r}")
        object.__setattr__(self, "rules", RollbackRules(tuple(self.rollback_for), tuple(self.no_rollback_for)))

    def rollback_on(self, error: BaseException) -> bool:
        """Whether *error*, leaving this boundary, rolls its unit back."""
        return self.rules.rollback_on(error)

    @property
    def label(self) -> str:
        """``name``, or the propagation's name, for logs."""
        return self.name or self.propagation.value
