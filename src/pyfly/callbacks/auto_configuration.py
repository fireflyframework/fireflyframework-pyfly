# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Auto-configuration for the callbacks module."""

from __future__ import annotations

from pyfly.callbacks.dispatcher import CallbackDispatcher
from pyfly.callbacks.repository import (
    InMemoryCallbackConfigRepository,
    InMemoryCallbackExecutionRepository,
)
from pyfly.config.auto import AutoConfiguration
from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration, conditional_on_property
from pyfly.core.config import Config


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
        config: Config,
    ) -> CallbackDispatcher:
        http = None
        if AutoConfiguration.is_available("httpx"):
            from pyfly.callbacks.adapters.httpx_sender import make_httpx_sender
            from pyfly.resilience.circuit_breaker import CircuitBreaker

            timeout: float = float(config.get("pyfly.callbacks.http.timeout", 10.0))

            failure_threshold_raw = config.get("pyfly.callbacks.http.circuit-breaker.failure-threshold", 5)
            recovery_timeout_raw = config.get("pyfly.callbacks.http.circuit-breaker.recovery-timeout", 30.0)

            breaker = CircuitBreaker(
                failure_threshold=int(failure_threshold_raw),
                recovery_timeout=float(recovery_timeout_raw),
            )
            http = make_httpx_sender(timeout=timeout, breaker=breaker)

        return CallbackDispatcher(configs=configs, executions=executions, http=http)
