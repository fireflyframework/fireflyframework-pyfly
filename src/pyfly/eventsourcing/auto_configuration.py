# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Auto-configuration for the event-sourcing module."""

from __future__ import annotations

from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration, conditional_on_property
from pyfly.eventsourcing.snapshot import InMemorySnapshotStore
from pyfly.eventsourcing.store import InMemoryEventStore


@auto_configuration
@conditional_on_property("pyfly.eventsourcing.enabled", having_value="true")
class EventSourcingAutoConfiguration:
    """Wire default event-sourcing beans into the DI container."""

    @bean
    def event_store(self) -> InMemoryEventStore:
        return InMemoryEventStore()

    @bean
    def snapshot_store(self) -> InMemorySnapshotStore:
        return InMemorySnapshotStore()
