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
"""Request mappings data provider."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext


class MappingsProvider:
    """Provides HTTP request mapping metadata."""

    def __init__(self, context: ApplicationContext) -> None:
        self._context = context

    @staticmethod
    def _extract_parameters(method_obj: Any) -> list[dict[str, str]]:
        """Extract handler parameters with name, type, and kind."""
        params: list[dict[str, str]] = []
        try:
            sig = inspect.signature(method_obj)
            hints = inspect.get_annotations(method_obj, eval_str=True)
        except (ValueError, TypeError):
            return params

        for name, param in sig.parameters.items():
            if name == "self":
                continue
            type_hint = hints.get(name)
            type_name = getattr(type_hint, "__name__", str(type_hint)) if type_hint else "Any"
            kind = "query"
            if param.default is not inspect.Parameter.empty:
                kind = "query"
            params.append({"name": name, "type": type_name, "kind": kind})
        return params

    @staticmethod
    def _extract_return_type(method_obj: Any) -> str | None:
        """Extract return type annotation from handler."""
        try:
            hints = inspect.get_annotations(method_obj, eval_str=True)
        except (ValueError, TypeError):
            return None
        ret = hints.get("return")
        if ret is None:
            return None
        return getattr(ret, "__name__", str(ret))

    async def get_mappings(self) -> dict[str, Any]:
        mappings: list[dict[str, Any]] = []
        for cls, _reg in self._context.container._registrations.items():
            if getattr(cls, "__pyfly_stereotype__", "") != "rest_controller":
                continue
            base_path = getattr(cls, "__pyfly_request_mapping__", "")
            tag = cls.__name__

            for attr_name in dir(cls):
                method_obj = getattr(cls, attr_name, None)
                if method_obj is None:
                    continue
                mapping = getattr(method_obj, "__pyfly_mapping__", None)
                if mapping is None:
                    continue

                full_path = base_path + mapping["path"]

                # Extract parameter kinds from path variables
                parameters = self._extract_parameters(method_obj)
                for p in parameters:
                    if f"{{{p['name']}}}" in full_path:
                        p["kind"] = "path"

                mappings.append(
                    {
                        "controller": tag,
                        "method": mapping["method"],
                        "path": full_path,
                        "handler": attr_name,
                        "status_code": mapping.get("status_code", 200),
                        "parameters": parameters,
                        "return_type": self._extract_return_type(method_obj),
                        "doc": inspect.getdoc(method_obj) or "",
                        "response_model": mapping.get("response_model", None),
                    }
                )
        mappings.sort(key=lambda m: m["path"])
        return {"mappings": mappings, "total": len(mappings)}
