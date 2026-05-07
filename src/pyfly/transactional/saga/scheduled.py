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
"""Scheduled saga / step-event annotations (mirror of @ScheduledSaga / @StepEvent)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ScheduledSaga:
    cron: str = ""
    fixed_rate_ms: int | None = None
    fixed_delay_ms: int | None = None
    initial_delay_ms: int = 0
    enabled: bool = True
    description: str = ""
    input: Any = None


@dataclass(frozen=True)
class StepEvent:
    event_type: str


def scheduled_saga(
    *,
    cron: str = "",
    fixed_rate_ms: int | None = None,
    fixed_delay_ms: int | None = None,
    initial_delay_ms: int = 0,
    enabled: bool = True,
    description: str = "",
    input: Any = None,
) -> Callable[[type], type]:
    """Decorate a ``@saga`` class to register it with the scheduler."""

    def decorator(cls: type) -> type:
        existing = list(getattr(cls, "__pyfly_saga_scheduled__", []))
        existing.append(
            ScheduledSaga(
                cron=cron,
                fixed_rate_ms=fixed_rate_ms,
                fixed_delay_ms=fixed_delay_ms,
                initial_delay_ms=initial_delay_ms,
                enabled=enabled,
                description=description,
                input=input,
            )
        )
        cls.__pyfly_saga_scheduled__ = existing  # type: ignore[attr-defined]
        return cls

    return decorator


def step_event(event_type: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a saga step's return value as a domain event of *event_type*."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.__pyfly_saga_step_event__ = StepEvent(event_type=event_type)  # type: ignore[attr-defined]
        return fn

    return decorator
