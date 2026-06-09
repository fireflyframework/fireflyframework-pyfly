# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""PluginState and PluginDescriptor — runtime state tracking for plugins."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from pyfly.plugins.decorators import Plugin


class PluginState(StrEnum):
    """Lifecycle state of a registered plugin."""

    LOADED = "LOADED"
    STARTED = "STARTED"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


@dataclass
class PluginDescriptor:
    """Runtime descriptor for a single plugin — its metadata + current state.

    Mutable so that ``PluginManager`` can transition ``state`` in place.
    """

    id: str
    plugin: Plugin
    state: PluginState
    loaded_at: datetime
    last_state_change: datetime
    failed_reason: str | None = field(default=None)
