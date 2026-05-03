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
"""AOP core types — JoinPoint dataclass."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class JoinPoint:
    """Represents a point in program execution where advice can be applied.

    Attributes:
        target: The object whose method is being intercepted.
        method_name: Name of the method being called.
        args: Positional arguments passed to the method.
        kwargs: Keyword arguments passed to the method.
        return_value: The return value (set after method execution).
        exception: Any exception raised during execution.
        proceed: Callable to invoke the original method (used in around advice).
    """

    target: Any
    method_name: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    return_value: Any = None
    exception: Exception | None = None
    proceed: Callable[..., Any] | None = None
