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
"""Resource-bundle message source — loads messages from YAML/JSON files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ResourceBundleMessageSource:
    """Resolves messages from locale-specific YAML or JSON resource files.

    File naming convention::

        {base_path}/messages_{locale}.yaml   (preferred)
        {base_path}/messages_{locale}.json   (fallback)

    Nested keys are flattened with dots, so the YAML structure::

        greeting:
          hello: "Hello, {0}!"

    is accessed as ``get_message("greeting.hello", ("World",), "en")``.
    """

    def __init__(
        self,
        base_path: str = "i18n/",
        default_locale: str = "en",
    ) -> None:
        self._base_path = Path(base_path)
        self._default_locale = default_locale
        self._cache: dict[str, dict[str, str]] = {}

    # ------------------------------------------------------------------
    # Public API (MessageSource protocol)
    # ------------------------------------------------------------------

    def get_message(
        self,
        code: str,
        args: tuple[Any, ...] = (),
        locale: str = "en",
    ) -> str:
        """Resolve *code* for *locale*, substituting positional *args*.

        Falls back to the default locale when *code* is missing in the
        requested locale.  Raises ``KeyError`` when the code cannot be
        found in either locale.
        """
        bundle = self._load_bundle(locale)
        template = bundle.get(code)

        if template is None and locale != self._default_locale:
            bundle = self._load_bundle(self._default_locale)
            template = bundle.get(code)

        if template is None:
            raise KeyError(f"No message found for code '{code}' in locale '{locale}'")

        return self._substitute(template, args)

    def get_message_or_default(
        self,
        code: str,
        default: str,
        args: tuple[Any, ...] = (),
        locale: str = "en",
    ) -> str:
        """Resolve *code* for *locale*, returning *default* on miss."""
        try:
            return self.get_message(code, args, locale)
        except KeyError:
            return self._substitute(default, args)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load_bundle(self, locale: str) -> dict[str, str]:
        """Load and cache the message bundle for *locale*."""
        if locale in self._cache:
            return self._cache[locale]

        messages: dict[str, str] = {}

        yaml_path = self._base_path / f"messages_{locale}.yaml"
        yml_path = self._base_path / f"messages_{locale}.yml"
        json_path = self._base_path / f"messages_{locale}.json"

        if yaml_path.is_file():
            messages = self._load_yaml(yaml_path)
        elif yml_path.is_file():
            messages = self._load_yaml(yml_path)
        elif json_path.is_file():
            messages = self._load_json(json_path)

        self._cache[locale] = messages
        return messages

    @staticmethod
    def _load_yaml(path: Path) -> dict[str, str]:
        import yaml  # type: ignore[import-untyped]

        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return _flatten(data)

    @staticmethod
    def _load_json(path: Path) -> dict[str, str]:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh) or {}
        return _flatten(data)

    @staticmethod
    def _substitute(template: str, args: tuple[Any, ...]) -> str:
        """Replace ``{0}``, ``{1}``, ... placeholders with *args*."""
        result = template
        for idx, arg in enumerate(args):
            result = result.replace(f"{{{idx}}}", str(arg))
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Flatten a nested dict into dot-separated keys with string values."""
    items: dict[str, str] = {}
    for key, value in data.items():
        full_key = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if isinstance(value, dict):
            items.update(_flatten(value, full_key))
        else:
            items[full_key] = str(value)
    return items
