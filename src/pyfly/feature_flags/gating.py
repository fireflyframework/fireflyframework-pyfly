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
"""``@feature_flag``: run a function, a method or every public method of a class only when a flag is on.

The gate asks the application's :class:`~pyfly.feature_flags.client.FeatureFlags` at call time, through the slot
the running context installs (:mod:`pyfly.feature_flags.slot`); without a running context every gate falls back to
its ``default``. A missing flag, an evaluation error and a ``DISABLED`` flag all give the caller's default, so gates
fail closed unless ``default=True`` (spec D8). With ``variant`` the gate is open when the flag resolves to that
variant. A closed gate calls ``fallback`` (a callable, or the name of a method of the same object, with the same
arguments) or raises :class:`FeatureFlagDisabledException` in the subclass whose HTTP status is
``pyfly.feature-flags.web.disabled-status`` (404, 403 or 503). The wrapper is a ``functools.wraps`` wrapper of the
same kind (sync or async), so route mappings, signatures and type hints are preserved.

No OpenFeature import here: the decorator is importable without the ``feature-flags`` extra.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

from pyfly.feature_flags.definitions import is_valid_key
from pyfly.feature_flags.slot import installed_feature_flags
from pyfly.kernel.exceptions import (
    ForbiddenException,
    PyFlyException,
    ResourceNotFoundException,
    ServiceUnavailableException,
)

__all__ = [
    "DISABLED_EXCEPTIONS",
    "FeatureFlagDisabledException",
    "FeatureFlagForbiddenException",
    "FeatureFlagNotFoundException",
    "FeatureFlagUnavailableException",
    "feature_flag",
    "feature_flag_disabled",
]

_T = TypeVar("_T")


class FeatureFlagDisabledException(PyFlyException):
    """A gated call ran while its flag was off. Raised as one of the three status subclasses."""

    def __init__(self, key: str) -> None:
        super().__init__(f"Feature '{key}' is not available", code="FEATURE_FLAG_DISABLED", context={"flag": key})
        self.key = key


class FeatureFlagNotFoundException(FeatureFlagDisabledException, ResourceNotFoundException):
    """``web.disabled-status: 404`` (the default): the feature does not exist for this caller."""


class FeatureFlagForbiddenException(FeatureFlagDisabledException, ForbiddenException):
    """``web.disabled-status: 403``."""


class FeatureFlagUnavailableException(FeatureFlagDisabledException, ServiceUnavailableException):
    """``web.disabled-status: 503``."""


DISABLED_EXCEPTIONS: dict[int, type[FeatureFlagDisabledException]] = {
    404: FeatureFlagNotFoundException,
    403: FeatureFlagForbiddenException,
    503: FeatureFlagUnavailableException,
}


def feature_flag_disabled(key: str, status: int = 404) -> FeatureFlagDisabledException:
    """The exception a closed gate on *key* raises under ``web.disabled-status`` *status*."""
    exception = DISABLED_EXCEPTIONS.get(status)
    if exception is None:
        raise ValueError(f"web.disabled-status must be 404, 403 or 503, got {status!r}")
    return exception(key)


@dataclass(frozen=True)
class _Gate:
    key: str
    variant: str | None
    default: bool
    fallback: str | Callable[..., Any] | None

    def is_open(self) -> bool:
        installed = installed_feature_flags()
        if installed is None:
            return self.default
        if self.variant is None:
            return installed.facade.is_enabled(self.key, default=self.default)
        details = installed.facade.variant_details(self.key)
        return (
            self.default
            if details.error_code is not None or details.variant is None
            else details.variant == self.variant
        )

    async def is_open_async(self) -> bool:
        installed = installed_feature_flags()
        if installed is None:
            return self.default
        if self.variant is None:
            return await installed.facade.is_enabled_async(self.key, default=self.default)
        details = await installed.facade.variant_details_async(self.key)
        return (
            self.default
            if details.error_code is not None or details.variant is None
            else details.variant == self.variant
        )

    def closed(
        self, args: tuple[Any, ...], kwargs: dict[str, Any], class_methods: Mapping[str, tuple[Any, Any]] | None = None
    ) -> Any:
        """Call the fallback (its result, possibly awaitable) or raise the disabled exception."""
        if callable(self.fallback):
            return self.fallback(*args, **kwargs)
        if isinstance(self.fallback, str):
            target = None
            if args:
                receiver = args[0]
                owner = receiver if isinstance(receiver, type) else type(receiver)
                if class_methods is not None and self.fallback in class_methods:
                    original, wrapped = class_methods[self.fallback]
                    if inspect.getattr_static(owner, self.fallback, None) is wrapped:
                        target = original.__get__(receiver, owner) if hasattr(original, "__get__") else original
                    else:
                        target = getattr(receiver, self.fallback, None)
                else:
                    target = getattr(receiver, self.fallback, None)
            if not callable(target):
                raise TypeError(f"@feature_flag({self.key!r}) fallback {self.fallback!r} is not a method of the target")
            return target(*args[1:], **kwargs)
        installed = installed_feature_flags()
        raise feature_flag_disabled(self.key, installed.disabled_status if installed is not None else 404)


def _wrap(
    function: Callable[..., Any], gate: _Gate, class_methods: Mapping[str, tuple[Any, Any]] | None = None
) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(function):

        @functools.wraps(function)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            if await gate.is_open_async():
                return await function(*args, **kwargs)
            result = gate.closed(args, kwargs, class_methods)
            return await result if inspect.isawaitable(result) else result

        async_wrapper.__pyfly_feature_flag__ = gate.key  # type: ignore[attr-defined]
        return async_wrapper

    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if gate.is_open():
            return function(*args, **kwargs)
        return gate.closed(args, kwargs, class_methods)

    wrapper.__pyfly_feature_flag__ = gate.key  # type: ignore[attr-defined]
    return wrapper


def _partial_callable(cls: type, name: str, attribute: functools.partialmethod[Any]) -> Callable[..., Any]:
    unbound = getattr(cls, name)
    if inspect.iscoroutinefunction(attribute.func):

        @functools.wraps(unbound)
        async def async_call(receiver: Any, *args: Any, **kwargs: Any) -> Any:
            owner = receiver if isinstance(receiver, type) else type(receiver)
            return await attribute.__get__(receiver, owner)(*args, **kwargs)

        return async_call

    @functools.wraps(unbound)
    def call(receiver: Any, *args: Any, **kwargs: Any) -> Any:
        owner = receiver if isinstance(receiver, type) else type(receiver)
        return attribute.__get__(receiver, owner)(*args, **kwargs)

    return call


def _wrap_class(cls: type, gate: _Gate) -> type:
    originals: dict[str, Any] = {}
    for name in dir(cls):
        if name.startswith("_"):
            continue
        attribute = inspect.getattr_static(cls, name)
        if (
            isinstance(attribute, staticmethod | classmethod)
            or inspect.isfunction(attribute)
            or inspect.ismethoddescriptor(attribute)
        ):
            originals[name] = attribute
    class_methods: dict[str, tuple[Any, Any]] = {}
    for name, attribute in originals.items():
        wrapped: Any
        if isinstance(attribute, staticmethod | classmethod):
            wrapped = type(attribute)(_wrap(attribute.__func__, gate, class_methods))
        elif isinstance(attribute, functools.partialmethod):
            wrapped = _wrap(_partial_callable(cls, name, attribute), gate, class_methods)
        else:
            wrapped = _wrap(attribute, gate, class_methods)
        setattr(cls, name, wrapped)
        class_methods[name] = attribute, wrapped
    return cls


def feature_flag(
    key: str,
    *,
    variant: str | None = None,
    default: bool = False,
    fallback: str | Callable[..., Any] | None = None,
) -> Callable[[_T], _T]:
    """Gate a function, a method, or every public method of a class on flag *key* (see the module documentation)."""
    if not is_valid_key(key):
        raise ValueError(f"@feature_flag: invalid flag key {key!r}")
    gate = _Gate(key, variant, default, fallback)

    def decorate(target: _T) -> _T:
        if isinstance(target, type):
            return _wrap_class(target, gate)  # type: ignore[return-value]
        if not callable(target):
            raise TypeError(f"@feature_flag decorates a function, a method or a class, not {target!r}")
        return _wrap(target, gate)  # type: ignore[return-value]

    return decorate
