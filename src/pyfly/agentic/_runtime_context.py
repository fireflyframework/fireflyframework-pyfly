# Copyright 2026 Firefly Software Foundation. Licensed under the Apache License, Version 2.0.
"""Context-local selection and lazy convenience services, without SDK imports."""

from __future__ import annotations

import threading
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

if TYPE_CHECKING:
    from pyfly.agentic.config import AgenticConfig
    from pyfly.agentic.runtime import AgenticRuntime
_current: ContextVar[AgenticRuntime | None] = ContextVar("pyfly_agentic_runtime", default=None)
_operation_config: ContextVar[tuple[AgenticRuntime, AgenticConfig] | None] = ContextVar(
    "pyfly_agentic_operation_config", default=None
)
T = TypeVar("T")


def selected_runtime() -> AgenticRuntime | None:
    return _current.get()


class RuntimeDefault(Generic[T]):
    """Resolve each access, including references imported before application startup."""

    def __init__(self, attribute: str, factory: Any) -> None:
        object.__setattr__(self, "_attribute", attribute)
        object.__setattr__(self, "_factory", factory)
        object.__setattr__(self, "_fallback", None)
        object.__setattr__(self, "_lock", threading.RLock())

    def resolve(self) -> T:
        runtime = selected_runtime()
        if runtime is not None:
            return cast(T, getattr(runtime, self._attribute))
        if self._fallback is None:
            with self._lock:
                if self._fallback is None:
                    object.__setattr__(self, "_fallback", self._factory())
        return cast(T, self._fallback)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.resolve(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self.resolve(), name, value)

    def __len__(self) -> int:
        return len(self.resolve())  # type: ignore[arg-type]

    def __contains__(self, item: Any) -> bool:
        return item in self.resolve()  # type: ignore[operator]

    def __bool__(self) -> bool:
        return bool(self.resolve())
