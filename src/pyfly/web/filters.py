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
"""OncePerRequestFilter — base class for WebFilter with URL-pattern matching.

Framework-agnostic: accesses ``request.url.path`` via attribute protocol
so no Starlette import is needed.
"""

from __future__ import annotations

import abc
from fnmatch import fnmatch
from typing import Any

from pyfly.web.ports.filter import CallNext


class OncePerRequestFilter(abc.ABC):
    """Abstract base class for :class:`WebFilter` implementations.

    Provides automatic URL-pattern matching via ``url_patterns`` and
    ``exclude_patterns``.  Subclasses only need to implement ``do_filter()``.

    Attributes:
        url_patterns: Glob patterns that this filter applies to.
            If empty (default), the filter applies to *all* paths.
        exclude_patterns: Glob patterns to exclude even if ``url_patterns``
            matches.  Checked *after* ``url_patterns``.
    """

    url_patterns: list[str] = []
    exclude_patterns: list[str] = []

    def should_not_filter(self, request: Any) -> bool:
        """Return ``True`` if the request path does not match this filter's patterns."""
        path: str = request.url.path

        # If url_patterns are set, at least one must match
        if self.url_patterns and not any(fnmatch(path, p) for p in self.url_patterns):
            return True

        # If any exclude pattern matches, skip
        return bool(self.exclude_patterns and any(fnmatch(path, p) for p in self.exclude_patterns))

    @abc.abstractmethod
    async def do_filter(self, request: Any, call_next: CallNext) -> Any:
        """Execute the filter logic.  Must call ``await call_next(request)`` to proceed."""
        ...
