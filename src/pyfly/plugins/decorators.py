# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Plugin / extension decorators."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Plugin:
    id: str
    version: str = "0.0.0"
    depends_on: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class ExtensionPoint:
    id: str
    description: str = ""


@dataclass(frozen=True)
class Extension:
    point: str
    priority: int = 0


def plugin(
    *, id: str, version: str = "0.0.0", depends_on: tuple[str, ...] = (), description: str = ""
) -> Callable[[type], type]:
    def decorator(cls: type) -> type:
        cls.__pyfly_plugin__ = Plugin(  # type: ignore[attr-defined]
            id=id, version=version, depends_on=depends_on, description=description
        )
        return cls

    return decorator


def extension_point(*, id: str, description: str = "") -> Callable[[type], type]:
    def decorator(cls: type) -> type:
        cls.__pyfly_extension_point__ = ExtensionPoint(id=id, description=description)  # type: ignore[attr-defined]
        return cls

    return decorator


def extension(*, point: str, priority: int = 0) -> Callable[[type], type]:
    def decorator(cls: type) -> type:
        cls.__pyfly_extension__ = Extension(point=point, priority=priority)  # type: ignore[attr-defined]
        return cls

    return decorator
