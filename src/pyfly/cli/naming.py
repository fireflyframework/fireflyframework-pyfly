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
"""Name-case derivation for code generators."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SPLIT_RE = re.compile(r"[^a-zA-Z0-9]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _words(raw: str) -> list[str]:
    """Split an identifier of any style into lowercase words."""
    spaced = _SPLIT_RE.sub(" ", raw)
    spaced = _CAMEL_RE.sub(" ", spaced)
    return [w.lower() for w in spaced.split() if w]


def _pluralize(word: str) -> str:
    """Naive English pluralization good enough for identifiers."""
    if word.endswith("y") and word[-2:-1] not in "aeiou":
        return word[:-1] + "ies"
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    return word + "s"


@dataclass(frozen=True)
class Names:
    """All case variants of an identifier the generators need."""

    raw: str
    pascal: str
    snake: str
    kebab: str
    camel: str
    snake_plural: str
    kebab_plural: str
    pascal_plural: str
    human: str
    human_plural: str


def names(raw: str) -> Names:
    """Derive every case variant from a single user-supplied name."""
    words = _words(raw)
    if not words:
        raise ValueError(f"Cannot derive a name from {raw!r}")
    snake = "_".join(words)
    plural_words = [*words[:-1], _pluralize(words[-1])]
    snake_plural = "_".join(plural_words)
    pascal = "".join(w.capitalize() for w in words)
    camel = words[0] + "".join(w.capitalize() for w in words[1:])
    return Names(
        raw=raw,
        pascal=pascal,
        snake=snake,
        kebab="-".join(words),
        camel=camel,
        snake_plural=snake_plural,
        kebab_plural="-".join(plural_words),
        pascal_plural="".join(w.capitalize() for w in plural_words),
        human=" ".join(words),
        human_plural=" ".join(plural_words),
    )
