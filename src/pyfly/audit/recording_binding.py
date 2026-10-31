# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Lazy per-context enrollment for native module adapters, never a process registry."""

import hashlib
import threading
from collections.abc import Callable
from typing import Any

from pyfly.audit.ports import AuditError
from pyfly.audit.recording import AuditRecorder
from pyfly.audit.recording_context import AuditAdapterEnrollment, AuditRecordingCoverage

AUTOMATIC_AUDIT_MODULES = frozenset({"data", "web", "security", "eda", "agentic"})


class AuditRecordingSupport:
    _pyfly_audit_factory: Callable[[], AuditRecorder] | None = None
    __pyfly_audit_module__: str | None = None

    def _bind_audit_recorder(self, factory: Callable[[], AuditRecorder]) -> None:
        existing = self.__dict__.setdefault("_pyfly_audit_factory", factory)
        if existing is not factory:
            raise AuditError("An audit hook instance cannot be rebound to another application")

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
    def __init__(self, factory: Callable[[], AuditRecorder], *, modules: frozenset[str] = frozenset()) -> None:
        self._factory = factory
        self._modules = modules
        self._adapters: set[tuple[str, str]] = set()
        self._lock = threading.RLock()

    def before_init(self, bean: Any, bean_name: str) -> Any:
        from pyfly.container.scoped_proxy import is_scoped_proxy

        if is_scoped_proxy(bean):
            return bean
        if isinstance(bean, AuditRecordingSupport):
            bean._bind_audit_recorder(self._factory)
            module = bean.__pyfly_audit_module__
            if module in self._modules:
                adapter = f"{type(bean).__module__}.{type(bean).__name__}"
                if len(adapter) > 128:
                    adapter = "class." + hashlib.sha256(adapter.encode()).hexdigest()
                with self._lock:
                    self._adapters.add((module, adapter))
        return bean

    def after_init(self, bean: Any, bean_name: str) -> Any:
        return self.before_init(bean, bean_name)

    def coverage(self) -> AuditRecordingCoverage:
        with self._lock:
            return AuditRecordingCoverage(
                configured_modules=tuple(sorted(self._modules)),
                di_bound_adapters=tuple(
                    AuditAdapterEnrollment(module=module, adapter=adapter) for module, adapter in sorted(self._adapters)
                ),
            )
