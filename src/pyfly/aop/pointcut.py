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
"""Pointcut expression matching for AOP advice targeting."""

from __future__ import annotations

import re


def matches_pointcut(pattern: str, qualified_name: str) -> bool:
    """Check whether *qualified_name* matches a pointcut *pattern*.

    Pattern syntax
    --------------
    * ``*``  — matches exactly one dot-separated segment.
    * ``**`` — matches one or more segments (crosses dots).
    * Partial globs in the **last** segment use fnmatch rules,
      e.g. ``get_*`` matches ``get_order``.

    Examples
    --------
    >>> matches_pointcut("service.*.*", "service.OrderService.create")
    True
    >>> matches_pointcut("**.*Service.*", "a.b.c.OrderService.create")
    True
    >>> matches_pointcut("mymod.MyClass.get_*", "mymod.MyClass.get_order")
    True
    >>> matches_pointcut("*.my_method", "a.b.MyClass.my_method")
    False
    """
    regex = _pattern_to_regex(pattern)
    return regex.fullmatch(qualified_name) is not None


def _segment_to_regex(seg: str) -> str:
    """Convert a single pattern segment to a regex fragment.

    Handles literal text, ``*`` (single segment), ``**`` (any depth),
    and partial globs like ``get_*`` or ``*Service``.
    """
    if seg == "**":
        # Matches one or more dot-separated segments.
        return r"(?:[^.]+\.)*[^.]+"
    if seg == "*":
        # Matches exactly one segment (no dots).
        return r"[^.]+"

    # Partial glob — translate character-by-character, keeping
    # ``*`` as ``[^.]*`` (within one segment) and escaping everything else.
    parts: list[str] = []
    i = 0
    while i < len(seg):
        ch = seg[i]
        if ch == "*":
            parts.append("[^.]*")
        elif ch == "?":
            parts.append("[^.]")
        else:
            parts.append(re.escape(ch))
        i += 1
    return "".join(parts)


def _pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """Convert a pointcut pattern string into a compiled regex."""
    segments = pattern.split(".")
    regex_parts = [_segment_to_regex(seg) for seg in segments]
    full = r"\.".join(regex_parts)
    return re.compile(full)
