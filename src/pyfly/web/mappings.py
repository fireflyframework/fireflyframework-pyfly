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
"""HTTP method mapping decorators for class-based controllers.

Mirrors Spring Boot's @RequestMapping, @GetMapping, @PostMapping, etc.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, TypeVar

T = TypeVar("T", bound=type)
F = TypeVar("F", bound=Callable[..., Any])


class MethodMapping(Protocol):
    """Typed signature of an HTTP method mapping decorator factory.

    Calling it (e.g. ``get_mapping("/path")``) returns a decorator that
    attaches routing metadata to the handler method.
    """

    def __call__(self, path: str = ..., *, status_code: int = ...) -> Callable[[F], F]: ...


def request_mapping(path: str) -> Callable[[T], T]:
    """Class-level decorator that sets the base path for all handler methods."""

    def decorator(cls: T) -> T:
        cls.__pyfly_request_mapping__ = path.rstrip("/")  # type: ignore[attr-defined]
        return cls

    return decorator


def _make_method_mapping(method: str) -> MethodMapping:
    """Factory that creates an HTTP method mapping decorator."""

    def mapping(path: str = "", *, status_code: int = 200) -> Callable[[F], F]:
        def decorator(func: F) -> F:
            func.__pyfly_mapping__ = {  # type: ignore[attr-defined]
                "method": method,
                "path": path,
                "status_code": status_code,
            }
            return func

        return decorator

    mapping.__name__ = f"{method.lower()}_mapping"
    mapping.__qualname__ = f"{method.lower()}_mapping"
    return mapping


def deprecated(func: F) -> F:
    """Mark an endpoint handler as deprecated in OpenAPI documentation."""
    func.__pyfly_deprecated__ = True  # type: ignore[attr-defined]
    return func


get_mapping = _make_method_mapping("GET")
post_mapping = _make_method_mapping("POST")
put_mapping = _make_method_mapping("PUT")
patch_mapping = _make_method_mapping("PATCH")
delete_mapping = _make_method_mapping("DELETE")
