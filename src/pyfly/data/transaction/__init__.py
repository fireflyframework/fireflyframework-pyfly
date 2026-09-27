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
"""Backend-neutral unit of work and transaction management.

Every transactional boundary is a :class:`UnitOfWork` bound to the running task through one
``ContextVar``; singleton beans are never mutated. ``@transactional`` gives Spring's semantics on every
backend (all seven propagations, rollback-only, additive rollback rules, isolation, read-only, timeouts,
a datasource qualifier, class-level decoration and synchronizations), and a repository call made outside
a transaction gets a short unit of its own that always commits or rolls back and releases its connection.

This package imports neither SQLAlchemy nor pymongo. The backend adapters implement
:class:`TransactionManager`: ``pyfly.data.relational.sqlalchemy.transaction_manager.SqlAlchemyTransactionManager``
(one per datasource of the ``DataSourceRegistry``) and the document module's MongoDB manager.

Main entry points:

- :func:`transactional` and :class:`TransactionTemplate` (``async with template.transaction():``);
- :func:`register_synchronization`, :func:`after_commit`, :func:`on_phase`;
- :func:`infrastructure_unit` for framework adapters (join the bound unit or open a short one);
- :func:`detached` (a function and a decorator) for work that must not join its caller's unit;
- :func:`outside_transaction` for a block of the calling task that must not join its units either;
- :func:`current_unit_of_work`, :func:`is_transaction_active`.
"""

from pyfly.data.transaction.context import (
    TransactionState,
    current_unit_of_work,
    detached,
    is_current_transaction_read_only,
    is_transaction_active,
    outside_transaction,
)
from pyfly.data.transaction.decorator import is_transactional, transactional
from pyfly.data.transaction.definition import Isolation, Propagation, TransactionDefinition
from pyfly.data.transaction.errors import (
    CommitOutcomeUnknownError,
    IllegalTransactionStateError,
    NestedTransactionNotSupportedError,
    TransactionError,
    TransactionSystemError,
    TransactionTimedOutError,
    UnexpectedRollbackError,
)
from pyfly.data.transaction.manager import TransactionCapabilities, TransactionManager
from pyfly.data.transaction.registry import (
    TransactionManagerRegistry,
    install_registry,
    installed_registry,
    resolve_manager,
    uninstall_registry,
)
from pyfly.data.transaction.rules import RollbackRules, rollback_on
from pyfly.data.transaction.synchronization import (
    CompletionStatus,
    TransactionPhase,
    TransactionSynchronization,
    TransactionSynchronizationAdapter,
    after_commit,
    on_phase,
    register_synchronization,
)
from pyfly.data.transaction.template import (
    TransactionTemplate,
    auto_unit,
    execute_in_transaction,
    infrastructure_unit,
)
from pyfly.data.transaction.unit_of_work import UnitOfWork, UnitStatus

__all__ = [
    "CommitOutcomeUnknownError",
    "CompletionStatus",
    "IllegalTransactionStateError",
    "Isolation",
    "NestedTransactionNotSupportedError",
    "Propagation",
    "RollbackRules",
    "TransactionCapabilities",
    "TransactionDefinition",
    "TransactionError",
    "TransactionManager",
    "TransactionManagerRegistry",
    "TransactionPhase",
    "TransactionState",
    "TransactionSynchronization",
    "TransactionSynchronizationAdapter",
    "TransactionSystemError",
    "TransactionTemplate",
    "TransactionTimedOutError",
    "UnexpectedRollbackError",
    "UnitOfWork",
    "UnitStatus",
    "after_commit",
    "auto_unit",
    "current_unit_of_work",
    "detached",
    "execute_in_transaction",
    "infrastructure_unit",
    "install_registry",
    "installed_registry",
    "is_current_transaction_read_only",
    "is_transaction_active",
    "is_transactional",
    "on_phase",
    "outside_transaction",
    "register_synchronization",
    "resolve_manager",
    "rollback_on",
    "transactional",
    "uninstall_registry",
]
