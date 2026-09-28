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
"""The state a repository write changes on a document, and the unit's synchronization that gives it back.

``MongoRepository.save`` and ``save_all`` make ids and revisions on the client. They become the documents' only once
the server stores them: a write that fails gives the documents their :class:`DocumentState` back at once, and a unit
that runs a transaction keeps the state of every document its repository writes stored (:func:`written`), and gives
it back when it rolls back (:class:`RestoreOnRollback`). The manager registers that synchronization when the
transaction starts (:func:`register_restorer`), so it is the unit's first one: it runs before any after-rollback
callback of the application, and nothing is inserted into the unit's list later.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from pyfly.data.transaction.synchronization import CompletionStatus, TransactionSynchronizationAdapter
from pyfly.data.transaction.unit_of_work import UnitOfWork

_logger = logging.getLogger(__name__)

WRITTEN_DOCUMENTS = "pyfly_mongo_written_documents"
"""``UnitOfWork.attributes`` key: the :class:`RestoreOnRollback` of a unit that runs a transaction."""


class DocumentState:
    """What a save changes on a document that must match the server's copy: its id, its revision, and its saved
    state (Beanie's state management)."""

    __slots__ = ("document", "id", "previous_saved_state", "revision_id", "saved_state")

    def __init__(self, document: Any) -> None:
        self.document = document
        self.id = document.id
        self.revision_id = document.revision_id
        self.saved_state = document._saved_state
        self.previous_saved_state = document._previous_saved_state

    def restore(self) -> None:
        """Give the document back the state it had when this snapshot was taken."""
        document = self.document
        document.id = self.id
        document.revision_id = self.revision_id
        document._saved_state = self.saved_state
        document._previous_saved_state = self.previous_saved_state


class RestoreOnRollback(TransactionSynchronizationAdapter):
    """Gives the documents a transaction wrote the state they had before its first write of them, when the
    transaction rolls back: none of those writes is stored. A commit, or one whose outcome is unknown, leaves them as
    the writes left them; either way the completed unit keeps no document alive."""

    def __init__(self) -> None:
        self.states: dict[int, DocumentState] = {}

    def remember(self, states: Iterable[DocumentState]) -> None:
        for state in states:
            self.states.setdefault(id(state.document), state)

    async def after_completion(self, status: CompletionStatus) -> None:
        states, self.states = self.states, {}
        if status is CompletionStatus.ROLLED_BACK:
            failure = restore(states.values())
            if failure is not None:
                raise failure  # logged and counted by the unit, as any synchronization failure


def register_restorer(unit: UnitOfWork) -> None:
    """Give *unit*, whose transaction just started, the synchronization that restores what its repository writes
    (its first one: the unit's list is still empty)."""
    restorer = RestoreOnRollback()
    unit.register_synchronization(restorer)
    unit.attributes[WRITTEN_DOCUMENTS] = restorer


def snapshots(documents: Iterable[Any]) -> list[DocumentState]:
    """The state of *documents* before a save writes them."""
    return [DocumentState(document) for document in documents]


def written(unit: UnitOfWork, states: Iterable[DocumentState]) -> None:
    """Record that *unit* wrote the documents of *states* (their state before the write): in a transaction, its
    rollback gives that state back. Only a write that succeeded is recorded, so a rollback never undoes what another
    unit stored after a save that failed here."""
    restorer = unit.attributes.get(WRITTEN_DOCUMENTS)
    if isinstance(restorer, RestoreOnRollback):
        restorer.remember(states)


def restore(states: Iterable[DocumentState]) -> Exception | None:
    """Give each document of *states* its state back; one that refuses it (a validator that runs on assignment)
    does not stop the others. Returns the first failure."""
    failure: Exception | None = None
    for state in states:
        try:
            state.restore()
        except Exception as error:  # noqa: BLE001 — every document gets its state back; the first failure is reported
            if failure is None:
                failure = error
    return failure


def restore_after(error: BaseException, states: Iterable[DocumentState]) -> None:
    """Give the documents of *states* their state back after a write failed with *error* (the error the caller
    re-raises: a restore that fails too is logged)."""
    failure = restore(states)
    if failure is not None:
        _logger.warning(
            "document_state_restore_failed",
            extra={"error": type(error).__name__},
            exc_info=(type(failure), failure, failure.__traceback__),
        )
