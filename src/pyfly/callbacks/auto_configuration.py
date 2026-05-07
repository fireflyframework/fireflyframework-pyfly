# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Auto-configuration for the callbacks module."""

from __future__ import annotations

from pyfly.callbacks.dispatcher import CallbackDispatcher
from pyfly.callbacks.repository import (
    InMemoryCallbackConfigRepository,
    InMemoryCallbackExecutionRepository,
)
from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration, conditional_on_property


@auto_configuration
@conditional_on_property("pyfly.callbacks.enabled", having_value="true")
class CallbacksAutoConfiguration:
    @bean
    def callback_config_repository(self) -> InMemoryCallbackConfigRepository:
        return InMemoryCallbackConfigRepository()

    @bean
    def callback_execution_repository(self) -> InMemoryCallbackExecutionRepository:
        return InMemoryCallbackExecutionRepository()

    @bean
    def callback_dispatcher(
        self,
        configs: InMemoryCallbackConfigRepository,
        executions: InMemoryCallbackExecutionRepository,
    ) -> CallbackDispatcher:
        return CallbackDispatcher(configs=configs, executions=executions)
