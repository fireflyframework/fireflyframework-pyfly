# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Lazy per-context enrollment for native module adapters, never a process registry."""

from collections.abc import Callable
from typing import Any

from pyfly.audit.ports import AuditError
from pyfly.audit.recording import AuditRecorder

AUTOMATIC_AUDIT_MODULES = frozenset({"data", "web", "security", "eda", "agentic"})


class AuditRecordingSupport:
    _pyfly_audit_factory: Callable[[], AuditRecorder] | None = None

    def _bind_audit_recorder(self, factory: Callable[[], AuditRecorder]) -> None:
        if self._pyfly_audit_factory is not None and self._pyfly_audit_factory is not factory:
            raise AuditError("An audit hook instance cannot be rebound to another application")
        self._pyfly_audit_factory = factory

    def _audit_recorder(self, module: str) -> AuditRecorder | None:
        factory = self._pyfly_audit_factory
        if factory is None:
            return None
        try:
            recorder = factory()
            return recorder if recorder.enabled(module) else None
        except BaseException as error:
            AuditRecorder._poison_active()
            if not isinstance(error, Exception):
                raise
            raise AuditError("Configured audit recording dependency is unavailable") from None


class AuditModulePostProcessor:
    def __init__(self, factory: Callable[[], AuditRecorder]) -> None:
        self._factory = factory

    def before_init(self, bean: Any, bean_name: str) -> Any:
        if isinstance(bean, AuditRecordingSupport):
            bean._bind_audit_recorder(self._factory)
        return bean

    def after_init(self, bean: Any, bean_name: str) -> Any:
        return self.before_init(bean, bean_name)
