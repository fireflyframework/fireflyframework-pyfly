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
"""Template engine and request context extension points."""

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable


class TemplateNotFoundError(LookupError):
    """The requested template does not exist in the configured loaders."""


@runtime_checkable
class TemplateEngine(Protocol):
    async def render(self, template: str, context: Mapping[str, Any]) -> str: ...


@runtime_checkable
class TemplateContextProcessor(Protocol):
    async def get_context(self, request: Any) -> Mapping[str, Any]: ...
