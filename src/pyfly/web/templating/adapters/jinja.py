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
"""Async Jinja renderer with constrained filesystem roots."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jinja2 import (
    ChoiceLoader,
    Environment,
    FileSystemLoader,
    PackageLoader,
    StrictUndefined,
    TemplateNotFound,
    Undefined,
    select_autoescape,
)

from pyfly.web.templating.config import TemplateProperties
from pyfly.web.templating.ports import TemplateNotFoundError


class _RootedLoader(FileSystemLoader):
    def get_source(self, environment: Environment, template: str) -> tuple[str, str, Any]:
        for root in self.searchpath:
            if not (Path(root) / template).resolve().is_relative_to(Path(root).resolve()):
                raise TemplateNotFound(template)
        source, filename, current = super().get_source(environment, template)
        resolved = Path(filename).resolve()
        if not any(resolved.is_relative_to(Path(root).resolve()) for root in self.searchpath):
            raise TemplateNotFound(template)
        return source, filename, current


class JinjaTemplateEngine:
    def __init__(self, properties: TemplateProperties) -> None:
        loaders: list[Any] = []
        for directory in properties.directories:
            if not Path(directory).is_dir():
                raise ValueError(f"Template directory does not exist: {directory}")
            loaders.append(_RootedLoader(directory))
        for location in properties.packages:
            package, _, directory = location.partition(":")
            loaders.append(PackageLoader(package, directory or "templates"))
        loaders.append(PackageLoader("pyfly.web.templating", "templates"))
        self.environment = Environment(
            loader=ChoiceLoader(loaders),
            enable_async=True,
            autoescape=select_autoescape(default_for_string=True, default=True),
            undefined=StrictUndefined if properties.strict_undefined else Undefined,
            auto_reload=properties.auto_reload,
            cache_size=properties.cache_size,
        )

    async def render(self, template: str, context: Mapping[str, Any]) -> str:
        try:
            return await self.environment.get_template(template).render_async(dict(context))
        except TemplateNotFound as exc:
            raise TemplateNotFoundError(str(exc)) from exc
