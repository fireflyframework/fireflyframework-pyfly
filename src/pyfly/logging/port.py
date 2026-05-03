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
"""LoggingPort — the hexagonal port for framework logging."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pyfly.core.config import Config


@runtime_checkable
class LoggingPort(Protocol):
    """Port defining the logging contract for PyFly."""

    def configure(self, config: Config) -> None: ...
    def get_logger(self, name: str) -> Any: ...
    def set_level(self, name: str, level: str) -> None: ...
