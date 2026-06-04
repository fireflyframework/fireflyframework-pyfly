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
"""ConfigProps actuator endpoint — Spring Boot ``/actuator/configprops`` parity.

Groups bound ``@config_properties`` classes by their prefix, showing the bound
(effective) values with secrets masked — like Spring Boot's ``@ConfigurationProperties``
report.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext

_CONFIG_PREFIX_ATTR = "__pyfly_config_prefix__"


def _to_plain(obj: Any) -> Any:
    """Convert a bound dataclass / Pydantic model to a JSON-friendly structure."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _to_plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(v) for v in obj]
    return obj


def _kebab_keys(value: Any) -> Any:
    """Recursively rename dict keys snake_case -> kebab-case (the YAML form).

    Spring shows bean property names; pyfly shows the kebab keys users actually
    write in ``pyfly.yaml`` (``event-loop``, not ``event_loop``)."""
    if isinstance(value, dict):
        return {str(k).replace("_", "-"): _kebab_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_kebab_keys(v) for v in value]
    return value


def _mask_tree(config: Any, prefix: str, value: Any) -> Any:
    """Recursively mask sensitive leaves under *prefix* using config rules."""
    if isinstance(value, dict):
        return {k: _mask_tree(config, f"{prefix}.{k}", v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_tree(config, prefix, v) for v in value]
    fn = getattr(config, "mask_value", None)
    return fn(prefix, value) if callable(fn) else value


# Framework-provided @config_properties classes always reported (if importable).
_FRAMEWORK_PROPERTY_CLASSES = (
    ("pyfly.config.properties.server", "ServerProperties"),
    ("pyfly.config.properties.web", "WebProperties"),
    ("pyfly.config.properties.data", "RelationalProperties"),
    ("pyfly.config.properties.cache", "CacheProperties"),
    ("pyfly.config.properties.client", "ClientProperties"),
    ("pyfly.config.properties.logging", "LoggingProperties"),
    ("pyfly.config.properties.messaging", "MessagingProperties"),
    ("pyfly.config.properties.mongodb", "DocumentProperties"),
)


class ConfigPropsEndpoint:
    """Exposes bound ``@config_properties`` beans at ``/actuator/configprops``."""

    def __init__(self, context: ApplicationContext) -> None:
        self._context = context

    @property
    def endpoint_id(self) -> str:
        return "configprops"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context: Any = None) -> dict[str, Any]:
        config = self._context.config
        beans: dict[str, Any] = {}

        for cls in self._candidate_classes():
            prefix = getattr(cls, _CONFIG_PREFIX_ATTR, None)
            if not prefix:
                continue
            try:
                bound: Any = config.bind(cls)
            except Exception:  # noqa: BLE001 - skip classes that fail to bind
                continue
            properties = _mask_tree(config, prefix, _kebab_keys(_to_plain(bound)))
            beans[cls.__name__] = {"prefix": prefix, "properties": properties}

        return {"contexts": {"application": {"beans": beans}}}

    def _candidate_classes(self) -> list[type]:
        seen: set[type] = set()
        classes: list[type] = []

        # 1. Framework property classes.
        import importlib

        for module_name, cls_name in _FRAMEWORK_PROPERTY_CLASSES:
            try:
                module = importlib.import_module(module_name)
                cls = getattr(module, cls_name, None)
            except ImportError:
                cls = None
            if cls is not None and cls not in seen:
                seen.add(cls)
                classes.append(cls)

        # 2. User @config_properties classes registered in the container.
        for cls in self._context.container._registrations:
            if getattr(cls, _CONFIG_PREFIX_ATTR, None) and cls not in seen:
                seen.add(cls)
                classes.append(cls)

        return classes
