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
"""MongoDB's persistence exceptions, translated to the kernel's.

Registered in the chain of :mod:`pyfly.data.exception_translation` when this module is imported (the
document repository imports it), so repositories, unit-of-work commits and the web layer raise one
backend-neutral hierarchy:

=================================================  =============================================================
MongoDB / Beanie exception                          Kernel exception (HTTP 409)
=================================================  =============================================================
duplicate key (``E11000``, codes 11000, 11001,      :class:`~pyfly.kernel.exceptions.DuplicateKeyException`,
12582), alone or in a bulk write                    naming the unique index
document validation failure (code 121)              :class:`~pyfly.kernel.exceptions.DataIntegrityException`
                                                    (a check violation)
``RevisionIdWasChanged`` (Beanie's revision)        :class:`~pyfly.kernel.exceptions.OptimisticLockingFailureException`
write conflict (code 112), or any error labeled     :class:`~pyfly.kernel.exceptions.ConcurrencyException`, a
``TransientTransactionError`` but an aborted        transient failure another attempt can get past
transaction (``NoSuchTransaction``, 251)
=================================================  =============================================================

The translated exception is raised from the driver's; its message names the index, never the duplicated
value (``dup key: { email: ... }`` may be personal data).

A write concern failure (pymongo's ``WriteConcernError``, ``WTimeoutError``: the server applied the write, and
the members the write concern asks for did not acknowledge it in time) is not translated, as the relational
translator leaves a connection failure: nothing is wrong with the data, no kernel exception means "applied, not
acknowledged", and a rule that names the driver's exception keeps working. The repository keeps a new document
that such a write stored with the id and revision it was stored with.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pyfly.data.exception_translation import (
    CHECK,
    UNIQUE,
    integrity_violation,
    optimistic_locking_failure,
    register_exception_translator,
)
from pyfly.kernel.exceptions import ConcurrencyException, PyFlyException

__all__ = ["MongoExceptionTranslator", "WRITE_CONFLICT_CODE"]

WRITE_CONFLICT_CODE = "WRITE_CONFLICT"
"""The ``code`` of a translated write conflict."""

_DUPLICATE_KEY_CODES = frozenset({11000, 11001, 12582})
_DOCUMENT_VALIDATION_FAILURE = 121
_WRITE_CONFLICT = 112
_NO_SUCH_TRANSACTION = 251
_INDEX = re.compile(r"index: (\S+)")


class MongoExceptionTranslator:
    """Translates pymongo's write errors and Beanie's revision conflicts (module documentation)."""

    def translate_exception_if_possible(self, error: BaseException) -> PyFlyException | None:
        try:
            from beanie.exceptions import RevisionIdWasChanged
            from pymongo.errors import BulkWriteError, OperationFailure, PyMongoError
        except ImportError:  # pragma: no cover — the document extra is not installed
            return None
        if isinstance(error, RevisionIdWasChanged):
            return optimistic_locking_failure()
        if not isinstance(error, PyMongoError):
            return None
        if isinstance(error, BulkWriteError):
            details: Mapping[str, Any] = error.details or {}
            for write_error in details.get("writeErrors", ()):
                translated = _translate_write_error(int(write_error.get("code", 0)), write_error)
                if translated is not None:
                    return translated
            return None
        if isinstance(error, OperationFailure):
            code = error.code or 0
            translated = _translate_write_error(code, error.details or {})
            if translated is not None:
                return translated
            if code == _WRITE_CONFLICT or (
                code != _NO_SUCH_TRANSACTION and error.has_error_label("TransientTransactionError")
            ):
                return ConcurrencyException(
                    "The document was changed by a concurrent transaction; retry the operation",
                    code=WRITE_CONFLICT_CODE,
                    context={"transient": True},
                )
        return None


def _translate_write_error(code: int, details: Mapping[str, Any]) -> PyFlyException | None:
    if code in _DUPLICATE_KEY_CODES:
        return integrity_violation(UNIQUE, _index_name(details))
    if code == _DOCUMENT_VALIDATION_FAILURE:
        return integrity_violation(CHECK, None)
    return None


def _index_name(details: Mapping[str, Any]) -> str | None:
    found = _INDEX.search(str(details.get("errmsg", "")))
    return found.group(1) if found else None


_TRANSLATOR = MongoExceptionTranslator()
register_exception_translator(_TRANSLATOR)
