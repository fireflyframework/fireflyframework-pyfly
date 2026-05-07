# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""PyFly outbound callbacks — webhooks pushed *to* external systems.

Mirrors ``org.fireflyframework.callbacks``: configs, subscriptions,
authorized domains, execution tracking, retry semantics.
"""

from __future__ import annotations

from pyfly.callbacks.dispatcher import CallbackDispatcher
from pyfly.callbacks.models import (
    AuthorizedDomain,
    CallbackConfig,
    CallbackExecution,
    CallbackStatus,
    CallbackSubscription,
)
from pyfly.callbacks.repository import (
    CallbackConfigRepository,
    CallbackExecutionRepository,
    InMemoryCallbackConfigRepository,
    InMemoryCallbackExecutionRepository,
)

__all__ = [
    "AuthorizedDomain",
    "CallbackConfig",
    "CallbackConfigRepository",
    "CallbackDispatcher",
    "CallbackExecution",
    "CallbackExecutionRepository",
    "CallbackStatus",
    "CallbackSubscription",
    "InMemoryCallbackConfigRepository",
    "InMemoryCallbackExecutionRepository",
]
