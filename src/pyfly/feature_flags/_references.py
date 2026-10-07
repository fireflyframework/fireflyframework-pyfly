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
"""What a ``$ref`` is (spec 4.1), shared by the provider, which expands references, and the registry, which follows
them to know which flags an evaluator change reaches. Plain Python, so the registry imports it without the extra."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["referenced_evaluator"]


def referenced_evaluator(node: Any) -> str | None:
    """The evaluator *node* references when it is a ``{"$ref": name}`` object (exactly one key, a text value)."""
    if isinstance(node, Mapping) and len(node) == 1:
        name = node.get("$ref")
        if isinstance(name, str):
            return name
    return None
