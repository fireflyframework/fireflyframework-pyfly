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
"""Rollback rules: whether an exception that leaves a unit of work rolls it back or lets it commit.

These are Spring's ``RuleBasedTransactionAttribute`` semantics, with every Python ``Exception`` in the
role of an unchecked exception:

- By default, any ``Exception`` rolls back.
- A ``BaseException`` that is not an ``Exception`` (``CancelledError``, ``KeyboardInterrupt``,
  ``SystemExit``) always rolls back; no rule can commit it.
- ``rollback_for`` adds rollback rules and ``no_rollback_for`` adds commit rules. They are *additive*:
  narrowing ``rollback_for`` to ``(PaymentError,)`` does not make a ``KeyError`` commit.
- When several rules match, the most specific one wins: the rule class closest to the exception's class
  in its MRO. A tie between a rollback rule and a commit rule rolls back.
"""

from __future__ import annotations

from dataclasses import dataclass

_VIRTUAL_DEPTH = 1 << 16
"""The depth of a rule that matches only through ``issubclass`` (an ABC's virtual subclass)."""


def rule_depth(exception_type: type[BaseException], rule: type[BaseException]) -> int | None:
    """How far *rule* sits from *exception_type* in its MRO (0: the class itself), or ``None`` when the
    rule does not match."""
    try:
        return exception_type.__mro__.index(rule)
    except ValueError:
        return _VIRTUAL_DEPTH if issubclass(exception_type, rule) else None


def _closest(exception_type: type[BaseException], rules: tuple[type[BaseException], ...]) -> int | None:
    depths = [depth for rule in rules if (depth := rule_depth(exception_type, rule)) is not None]
    return min(depths) if depths else None


@dataclass(frozen=True, slots=True)
class RollbackRules:
    """The rollback rules of a transaction definition."""

    rollback_for: tuple[type[BaseException], ...] = ()
    no_rollback_for: tuple[type[BaseException], ...] = ()

    def rollback_on(self, error: BaseException) -> bool:
        """Whether *error*, leaving the unit of work, rolls it back."""
        if not isinstance(error, Exception):
            return True
        commit_depth = _closest(type(error), self.no_rollback_for)
        if commit_depth is None:
            return True
        rollback_depth = _closest(type(error), self.rollback_for)
        return rollback_depth is not None and rollback_depth <= commit_depth


def rollback_on(
    error: BaseException,
    rollback_for: tuple[type[BaseException], ...] = (),
    no_rollback_for: tuple[type[BaseException], ...] = (),
) -> bool:
    """Whether *error* rolls back a unit declared with these rules (see the module documentation)."""
    return RollbackRules(rollback_for, no_rollback_for).rollback_on(error)
