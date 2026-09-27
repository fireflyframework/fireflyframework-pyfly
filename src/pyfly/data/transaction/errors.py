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
"""The transaction exceptions, one hierarchy for every backend (Spring's ``TransactionException`` family).

::

    TransactionError (InfrastructureException)
     ├─ UnexpectedRollbackError             the outermost boundary rolled back a rollback-only unit
     ├─ IllegalTransactionStateError        propagation or usage error (also a RuntimeError)
     ├─ TransactionTimedOutError            the unit's timeout expired (also a TimeoutError)
     ├─ NestedTransactionNotSupportedError  NESTED on a backend without savepoints
     └─ TransactionSystemError              the backend failed while completing a unit
         └─ CommitOutcomeUnknownError       a commit was interrupted in flight; never retry it
"""

from __future__ import annotations

from typing import Any

from pyfly.kernel.exceptions import InfrastructureException


class TransactionError(InfrastructureException):
    """Base class of every transaction error; ``context["datasource"]`` names the datasource when known."""

    def __init__(self, message: str, *, datasource: str | None = None, context: dict[str, Any] | None = None) -> None:
        merged = dict(context or {})
        if datasource is not None:
            merged.setdefault("datasource", datasource)
        super().__init__(message, code=type(self).__name__, context=merged)

    @property
    def datasource(self) -> str | None:
        """The datasource of the unit of work the error is about, when known."""
        value = self.context.get("datasource")
        return str(value) if value is not None else None


class UnexpectedRollbackError(TransactionError):
    """The outermost boundary completed normally, but the unit had been marked rollback-only.

    A participant (a joined ``@transactional`` call, a repository call inside the unit) failed with an
    exception its rollback rules roll back, or a statement failed and left the transaction unusable, and
    the caller caught the error and carried on. The unit rolled back instead of committing partial work.
    """


class IllegalTransactionStateError(TransactionError, RuntimeError):
    """A propagation rule or a usage rule was broken (``MANDATORY`` without a unit, ``NEVER`` inside one,
    a unit used after it completed, an unsupported isolation level, an ambiguous transaction manager)."""


class TransactionTimedOutError(TransactionError, TimeoutError):
    """The unit's ``timeout`` expired; the unit rolled back."""


class NestedTransactionNotSupportedError(TransactionError):
    """``Propagation.NESTED`` joined a unit whose backend has no savepoints (MongoDB)."""


class TransactionSystemError(TransactionError):
    """The backend failed while completing a unit of work (commit, rollback or release)."""


class CommitOutcomeUnknownError(TransactionSystemError):
    """The connection failed while ``COMMIT`` was in flight: the unit may or may not have committed.

    Never retry it blindly: a retried unit of work would apply its writes twice when the first commit
    did land. :func:`pyfly.resilience.retry` never retries it (``retryable = False``). Reconcile instead (an
    idempotency key, a read of the written rows) or let the transactional outbox deliver exactly once.
    """

    retryable = False
    """Read by ``@retry``: an unknown commit outcome is never retried."""
