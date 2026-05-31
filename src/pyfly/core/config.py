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
"""Type-safe configuration with YAML/TOML files, env vars, and dataclass binding."""

from __future__ import annotations

import dataclasses
import importlib.resources
import os
import re
import tomllib
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar, Union, cast, get_args, get_origin, get_type_hints

import yaml  # type: ignore[import-untyped]

T = TypeVar("T")

_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")

_CONFIG_PROPERTIES_ATTR = "__pyfly_config_prefix__"


def config_properties(prefix: str) -> Callable[[type[T]], type[T]]:
    """Mark a class as bindable to a configuration prefix.

    Works with both dataclasses and Pydantic BaseModel subclasses.
    When used with Pydantic models, Config.bind() uses model_validate()
    for automatic type coercion, nested model support, and fail-fast
    validation at startup.

    Usage:
        @config_properties(prefix="database")
        @dataclass
        class DatabaseConfig:
            url: str = "sqlite:///test.db"

        @config_properties(prefix="myapp.server")
        class ServerConfig(BaseModel):
            host: str = "0.0.0.0"
            port: int = Field(default=8080, ge=1, le=65535)
    """

    def decorator(cls: type[T]) -> type[T]:
        setattr(cls, _CONFIG_PROPERTIES_ATTR, prefix)
        return cls

    return decorator


class Config:
    """Hierarchical configuration with dot-notation access and env var overrides.

    Priority (highest wins):
    1. Environment variables (PYFLY_SECTION_KEY format)
    2. Configuration dict / YAML file values
    3. Dataclass defaults
    """

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = data or {}
        self._loaded_sources: list[str] = []

    @property
    def loaded_sources(self) -> list[str]:
        """List of config file paths that were loaded, in merge order."""
        return list(self._loaded_sources)

    def to_dict(self) -> dict[str, Any]:
        """Return a shallow copy of the raw configuration data."""
        return dict(self._data)

    @classmethod
    def from_sources(
        cls,
        base_dir: str | Path,
        active_profiles: list[str] | None = None,
        load_defaults: bool = True,
        starter_defaults: dict[str, Any] | None = None,
    ) -> Config:
        """Load and merge config from multiple sources (Spring Boot style).

        Merge order (later wins):
        1. Framework defaults (pyfly-defaults.yaml from package)
        2. *Starter defaults* — properties activated by ``@enable_*_stack``
           decorators (passed in via *starter_defaults*). They sit between
           the framework defaults and the user files so the bundle takes
           effect, while explicit user values still win.
        3. config/pyfly.yaml or config/pyfly.toml (config subdirectory)
        4. pyfly.yaml or pyfly.toml (project root)
        5. Profile overlays: config/pyfly-{profile}.yaml, pyfly-{profile}.yaml
        6. Environment variables (handled at read time in get())
        """
        base_dir = Path(base_dir)
        data: dict[str, Any] = {}
        sources: list[str] = []

        # 1. Framework defaults
        if load_defaults:
            data = cls._load_framework_defaults()
            sources.append("pyfly-defaults.yaml (framework defaults)")

        # 1b. Starter defaults (between framework defaults and user files)
        if starter_defaults:
            data = cls._deep_merge(data, starter_defaults)
            sources.append("starter defaults (@enable_*_stack)")

        # 2. config/ subdirectory
        for ext in (".yaml", ".toml"):
            candidate = base_dir / "config" / f"pyfly{ext}"
            if candidate.is_file():
                data = cls._deep_merge(data, cls._load_config_data(candidate))
                sources.append(str(candidate))

        # 3. Project root
        for ext in (".yaml", ".toml"):
            candidate = base_dir / f"pyfly{ext}"
            if candidate.is_file():
                data = cls._deep_merge(data, cls._load_config_data(candidate))
                sources.append(str(candidate))

        # 4. Profile overlays (from both locations)
        for profile in active_profiles or []:
            for search_dir in [base_dir / "config", base_dir]:
                for ext in (".yaml", ".toml"):
                    candidate = search_dir / f"pyfly-{profile}{ext}"
                    if candidate.is_file():
                        data = cls._deep_merge(data, cls._load_config_data(candidate))
                        sources.append(f"{candidate} (profile: {profile})")

        instance = cls(data)
        instance._loaded_sources = sources
        return instance

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        active_profiles: list[str] | None = None,
        load_defaults: bool = True,
    ) -> Config:
        """Load configuration from a YAML or TOML file (backward-compatible).

        If *path* is a standard pyfly config (pyfly.yaml / pyfly.toml),
        delegates to :meth:`from_sources` for full multi-source loading.
        Otherwise, loads the single file directly (preserving original
        behaviour for arbitrary config file names).
        """
        path = Path(path)

        # If the file follows the pyfly naming convention, use multi-source
        if path.stem in ("pyfly",) or path.stem.startswith("pyfly-"):
            return cls.from_sources(
                base_dir=path.parent,
                active_profiles=active_profiles,
                load_defaults=load_defaults,
            )

        # Fallback: load the exact file (original from_file behaviour)
        data: dict[str, Any] = {}
        sources: list[str] = []

        if load_defaults:
            data = cls._load_framework_defaults()
            sources.append("pyfly-defaults.yaml (framework defaults)")

        if path.exists():
            user_data = cls._load_config_data(path)
            data = cls._deep_merge(data, user_data)
            sources.append(str(path))

        if path.exists():
            for profile in active_profiles or []:
                profile_path = path.parent / f"{path.stem}-{profile}{path.suffix}"
                if profile_path.exists():
                    profile_data = cls._load_config_data(profile_path)
                    data = cls._deep_merge(data, profile_data)
                    sources.append(f"{profile_path} (profile: {profile})")

        instance = cls(data)
        instance._loaded_sources = sources
        return instance

    @staticmethod
    def _load_config_data(path: Path) -> dict[str, Any]:
        """Load config data from a YAML or TOML file."""
        if path.suffix == ".toml":
            with open(path, "rb") as f:
                return tomllib.load(f) or {}
        with open(path) as f:
            return yaml.safe_load(f) or {}

    @staticmethod
    def _load_framework_defaults() -> dict[str, Any]:
        """Load built-in framework defaults from pyfly.resources."""
        defaults_file = importlib.resources.files("pyfly.resources").joinpath("pyfly-defaults.yaml")
        with importlib.resources.as_file(defaults_file) as p, open(p) as f:
            return yaml.safe_load(f) or {}

    @staticmethod
    def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        """Recursively merge override into base, with override values winning."""
        merged = dict(base)
        for key, value in override.items():
            if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
                merged[key] = Config._deep_merge(merged[key], value)
            else:
                merged[key] = value
        return merged

    def get(self, key: str, default: Any = None) -> Any:
        """Get a value by dot-notation key, checking env vars first.

        String values containing ``${...}`` placeholders are resolved:
        - ``${ENV_VAR}`` — resolved from environment variables
        - ``${config.key}`` — resolved from other config values
        - ``${key:default}`` — uses default if key/env not found
        """
        # Check environment variable override: pyfly.app.name -> PYFLY_APP_NAME
        env_base = key.removeprefix("pyfly.") if key.startswith("pyfly.") else key
        env_key = "PYFLY_" + env_base.upper().replace(".", "_").replace("-", "_")
        env_val = os.environ.get(env_key)
        if env_val is not None:
            return env_val

        # Walk nested dict
        parts = key.split(".")
        current: Any = self._data
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
                if current is None:
                    return default
            else:
                return default

        # Resolve placeholders in string values
        if isinstance(current, str) and "${" in current:
            return self._resolve_placeholders(current)

        return current

    def _resolve_placeholders(self, value: str, _depth: int = 0) -> str:
        """Resolve ``${...}`` placeholders in a string value.

        Supports environment variables, config references, and defaults.
        Guards against circular references with a max recursion depth.
        """
        if _depth > 10:
            raise ValueError(
                f"Max recursion depth exceeded resolving placeholders in '{value}'. Check for circular references."
            )

        def _replace(match: re.Match[str]) -> str:
            inner = match.group(1)

            # Check for default: ${key:default}
            if ":" in inner:
                ref_key, default_val = inner.split(":", 1)
            else:
                ref_key, default_val = inner, None

            # Try environment variable first
            env_val = os.environ.get(ref_key)
            if env_val is not None:
                return env_val

            # Try config reference (raw, without placeholder resolution)
            parts = ref_key.split(".")
            current: Any = self._data
            for part in parts:
                if isinstance(current, dict):
                    current = current.get(part)
                    if current is None:
                        break
                else:
                    current = None
                    break

            if current is not None:
                resolved = str(current)
                # Recursively resolve if the resolved value also has placeholders
                if "${" in resolved:
                    resolved = self._resolve_placeholders(resolved, _depth + 1)
                return resolved

            if default_val is not None:
                return cast(str, default_val)

            raise ValueError(f"Cannot resolve placeholder '${{{inner}}}': not found in environment or config")

        return _PLACEHOLDER_RE.sub(_replace, value)

    def get_section(self, prefix: str) -> dict[str, Any]:
        """Get all values under a prefix as a flat dict."""
        parts = prefix.split(".")
        current: Any = self._data
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part, {})
            else:
                return {}
        return current if isinstance(current, dict) else {}

    def bind(self, config_cls: type[T]) -> T:
        """Bind configuration to a @config_properties dataclass or Pydantic model."""
        prefix = getattr(config_cls, _CONFIG_PROPERTIES_ATTR, None)
        if prefix is None:
            raise ValueError(f"{config_cls.__name__} is not decorated with @config_properties")

        # Resolve ``${...}`` placeholders across the whole section before binding.
        # ``_resolve_tree`` builds a fresh structure, so ``self._data`` stays raw
        # (single-value ``get()`` continues to resolve lazily).
        section = self._resolve_tree(self.get_section(prefix))

        # Pydantic BaseModel path — fail-fast with ValidationError
        try:
            from pydantic import BaseModel, ValidationError

            if isinstance(config_cls, type) and issubclass(config_cls, BaseModel):
                try:
                    return config_cls.model_validate(section)
                except ValidationError as exc:
                    raise ValueError(
                        f"Configuration validation failed for '{config_cls.__name__}' (prefix='{prefix}'):\n{exc}"
                    ) from exc
        except ImportError:
            pass

        # Dataclass path (recurses into nested dataclass fields).
        return cast(T, self._bind_dataclass(config_cls, section))

    def _resolve_tree(self, value: Any, _depth: int = 0) -> Any:
        """Recursively resolve ``${...}`` placeholders inside nested structures,
        returning a new structure without mutating the underlying config data."""
        if isinstance(value, str):
            return self._resolve_placeholders(value, _depth) if "${" in value else value
        if isinstance(value, dict):
            return {k: self._resolve_tree(v, _depth) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve_tree(v, _depth) for v in value]
        return value

    def _bind_dataclass(self, config_cls: Any, section: dict[str, Any]) -> Any:
        """Bind a (possibly nested) dataclass from an already placeholder-resolved dict."""
        hints = get_type_hints(config_cls)
        kwargs: dict[str, Any] = {}
        for field in dataclasses.fields(config_cls):
            if field.name not in section:
                continue
            kwargs[field.name] = self._coerce_value(hints.get(field.name), section[field.name])
        return config_cls(**kwargs)

    def _coerce_value(self, expected_type: Any, value: Any) -> Any:
        """Coerce a raw config value to the field's declared type, recursing into
        nested dataclasses (so e.g. ``ServerProperties.granian`` becomes a
        ``GranianProperties`` rather than a raw dict)."""
        # Unwrap Optional[T] / T | None to the underlying type.
        if get_origin(expected_type) is Union or isinstance(expected_type, types.UnionType):
            non_none = [a for a in get_args(expected_type) if a is not type(None)]
            if non_none:
                expected_type = non_none[0]

        if dataclasses.is_dataclass(expected_type) and isinstance(value, dict):
            return self._bind_dataclass(expected_type, value)
        if expected_type is int and isinstance(value, str):
            return int(value)
        if expected_type is float and isinstance(value, str):
            return float(value)
        if expected_type is bool and isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return value
