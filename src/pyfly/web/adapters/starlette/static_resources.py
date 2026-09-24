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
"""Configured static mounts, sharing the ASGI adapter's path protections."""

from pathlib import Path
from typing import Any

from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from pyfly.web.templating.config import StaticProperties


class ConfiguredStaticFiles(StaticFiles):
    def __init__(self, properties: StaticProperties) -> None:
        packages: list[str | tuple[str, str]] = []
        for value in properties.packages:
            package, _, folder = value.partition(":")
            packages.append((package, folder or "static"))
        super().__init__(packages=packages, follow_symlink=False)
        roots = [str(Path(path).resolve()) for path in properties.directories]
        for root in roots:
            if not Path(root).is_dir():
                raise ValueError(f"Static directory does not exist: {root}")
        self.all_directories = [*roots, *self.all_directories]
        self._cache_control = properties.cache_control

    async def get_response(self, path: str, scope: Any) -> Any:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = self._cache_control
        return response


def build_static_routes(properties: StaticProperties) -> list[Mount]:
    if not properties.enabled:
        return []
    path = properties.path.rstrip("/")
    if not path.startswith("/") or path.startswith("//") or not path:
        raise ValueError("Static path must be an absolute non-root URL path")
    return [Mount(path, app=ConfiguredStaticFiles(properties), name="static")]
