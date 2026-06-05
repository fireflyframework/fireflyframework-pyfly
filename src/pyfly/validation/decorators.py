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
"""Validation decorators for input validation and custom validators."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

from pyfly.kernel.exceptions import ValidationException
from pyfly.validation.helpers import validate_model

F = TypeVar("F", bound=Callable[..., Any])


def validate_input(model: type[BaseModel], param: str) -> Callable[[F], F]:
    """Decorator that validates a keyword argument against a Pydantic model.

    A ``dict`` is validated and replaced with the model instance; a value that is
    already an instance of *model* passes through; any other non-``None`` value is
    rejected with a :class:`ValidationException` (rather than silently passing
    through unvalidated). Works on both sync and async target functions.

    Args:
        model: Pydantic model class to validate against.
        param: Name of the keyword argument to validate.
    """

    def _coerce(kwargs: dict[str, Any]) -> None:
        value = kwargs.get(param)
        if value is None or isinstance(value, model):
            return
        if isinstance(value, dict):
            kwargs[param] = validate_model(model, value)
            return
        raise ValidationException(
            f"Invalid value for '{param}': expected {model.__name__} or a dict",
            code="VALIDATION_ERROR",
        )

    def decorator(func: F) -> F:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                _coerce(kwargs)
                return await func(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            _coerce(kwargs)
            return func(*args, **kwargs)

        return sync_wrapper  # type: ignore[return-value]

    return decorator


def validator(
    predicate: Callable[..., bool],
    message: str = "Validation failed",
) -> Callable[[F], F]:
    """Decorator that validates function arguments with a predicate.

    The predicate receives the same arguments as the decorated function. If it
    returns ``False`` a :class:`ValidationException` is raised. Works on both sync
    and async target functions.

    Args:
        predicate: Function that returns True if valid.
        message: Error message on failure.
    """

    def decorator(func: F) -> F:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                if not predicate(*args, **kwargs):
                    raise ValidationException(message, code="VALIDATION_ERROR")
                return await func(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            if not predicate(*args, **kwargs):
                raise ValidationException(message, code="VALIDATION_ERROR")
            return func(*args, **kwargs)

        return sync_wrapper  # type: ignore[return-value]

    return decorator
