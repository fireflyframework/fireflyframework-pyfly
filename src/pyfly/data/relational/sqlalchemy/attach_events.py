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
"""Hearing about the instances a session takes in (``session.add`` of a new or a detached instance).

Framework code outside the relational module (the domain-event publisher of :mod:`pyfly.eda.domain_events`)
needs to know when a unit of work's session takes in an aggregate: a new aggregate saved, or a detached one
re-attached by ``Repository.save``. :func:`listen_for_attached` registers a listener on every session for
the two ORM events that say so, once per listener and process.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session

AttachListener = Callable[[Any, object], None]
"""Called with the (sync) ``Session`` and the instance it took in."""

_EVENTS = ("transient_to_pending", "detached_to_persistent")


def listen_for_attached(listener: AttachListener) -> None:
    """Call *listener* whenever any session takes in an instance (idempotent)."""
    for name in _EVENTS:
        if not event.contains(Session, name, listener):
            event.listen(Session, name, listener)


def stop_listening_for_attached(listener: AttachListener) -> None:
    """Stop calling *listener* (idempotent)."""
    for name in _EVENTS:
        if event.contains(Session, name, listener):
            event.remove(Session, name, listener)
