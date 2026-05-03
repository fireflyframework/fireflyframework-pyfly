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
"""Admin view registry -- manages built-in and custom dashboard views."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyfly.admin.ports import AdminViewExtension

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext


class AdminViewRegistry:
    """Collects and manages admin dashboard view extensions."""

    def __init__(self) -> None:
        self._extensions: dict[str, AdminViewExtension] = {}

    def register(self, extension: AdminViewExtension) -> None:
        self._extensions[extension.view_id] = extension

    def get_extensions(self) -> dict[str, AdminViewExtension]:
        return dict(self._extensions)

    def discover_from_context(self, context: ApplicationContext) -> None:
        for _cls, reg in context.container._registrations.items():
            if reg.instance is not None and isinstance(reg.instance, AdminViewExtension):
                ext = reg.instance
                if ext.view_id not in self._extensions:
                    self._extensions[ext.view_id] = ext
