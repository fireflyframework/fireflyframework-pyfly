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
"""Per-key composition of the flag layers (spec 4.5).

The highest layer that defines a key supplies its whole definition (never a field-by-field merge) and records the
lower layers it shadows; ``$evaluators`` merge by name and document ``metadata`` by key, with the same precedence.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pyfly.feature_flags.definitions import FlagDocument

__all__ = ["ComposedFlag", "Composition", "Layer", "compose"]


@dataclass(frozen=True)
class Layer:
    """One source's validated document, named after the source."""

    source: str
    document: FlagDocument


@dataclass(frozen=True)
class ComposedFlag:
    """A flag of the effective set: its definition comes from ``origin``; ``overrides`` lists what it shadows."""

    key: str
    definition: dict[str, Any]
    origin: str
    overrides: tuple[str, ...] = ()


@dataclass(frozen=True)
class Composition:
    """The effective flag set."""

    flags: dict[str, ComposedFlag] = field(default_factory=dict)
    evaluators: dict[str, dict[str, Any]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_flagd(self) -> dict[str, Any]:
        """The effective set as a flagd document: keys sorted (a stable ETag), deep copies."""
        document: dict[str, Any] = {
            "flags": {key: copy.deepcopy(self.flags[key].definition) for key in sorted(self.flags)}
        }
        if self.evaluators:
            document["$evaluators"] = {name: copy.deepcopy(self.evaluators[name]) for name in sorted(self.evaluators)}
        if self.metadata:
            document["metadata"] = dict(sorted(self.metadata.items()))
        return document


def compose(layers: Sequence[Layer]) -> Composition:
    """Compose *layers*, lowest precedence first."""
    flags: dict[str, ComposedFlag] = {}
    evaluators: dict[str, dict[str, Any]] = {}
    metadata: dict[str, Any] = {}
    for layer in layers:
        for key, definition in layer.document.flags.items():
            below = flags.get(key)
            shadowed = (*below.overrides, below.origin) if below is not None else ()
            flags[key] = ComposedFlag(key, copy.deepcopy(definition), layer.source, shadowed)
        for name, rule in layer.document.evaluators.items():
            evaluators[name] = copy.deepcopy(rule)
        metadata.update(layer.document.metadata)
    return Composition(flags=flags, evaluators=evaluators, metadata=metadata)
