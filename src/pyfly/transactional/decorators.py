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
"""Transactional engine decorators."""

from __future__ import annotations


def enable_transactional_engine(cls: type) -> type:
    """Enable the transactional engine for a configuration class.

    Sets ``__pyfly_enable_transactional_engine__`` on the class so that
    the auto-configuration module can detect and activate the engine.

    Args:
        cls: The configuration class to annotate.

    Returns:
        The same class with the marker attribute set.
    """
    cls.__pyfly_enable_transactional_engine__ = True  # type: ignore[attr-defined]
    return cls
