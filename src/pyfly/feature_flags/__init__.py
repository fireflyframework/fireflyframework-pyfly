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
"""Feature flags on OpenFeature and flagd: the Firefly contract (see docs/modules/feature-flags.md).

The public names load on first use (PEP 562), so ``import pyfly.feature_flags`` — and the decorator, the
definitions and the test support — work without the ``feature-flags`` extra; only the names that need OpenFeature
import it, when they are first used.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.feature_flags.client import FeatureFlags, OpenFeatureBinding
    from pyfly.feature_flags.composition import ComposedFlag, Composition, Layer, compose
    from pyfly.feature_flags.context import (
        ApplicationContextContributor,
        EvaluationContextContributor,
        EvaluationContextResolver,
        FeatureFlagsContextFilter,
        SecurityContextContributor,
        TenantContextContributor,
    )
    from pyfly.feature_flags.definitions import (
        FlagDefinitionError,
        FlagDocument,
        normalize_flags,
        parse_document,
        validate_flag,
    )
    from pyfly.feature_flags.events import FeatureFlagEvaluated, FeatureFlagsChanged, FeatureFlagUpdated
    from pyfly.feature_flags.hooks import ExposureEventHook, MetricsHook
    from pyfly.feature_flags.properties import FeatureFlagsProperties
    from pyfly.feature_flags.provider import FireflyFlagProvider
    from pyfly.feature_flags.registry import FeatureFlagsError, FlagRegistry, SourceStatus
    from pyfly.feature_flags.slot import install_feature_flags, installed_feature_flags, uninstall_feature_flags
    from pyfly.feature_flags.sources import FlagSource, FlagSourceError, SourceSnapshot
    from pyfly.feature_flags.sources.config import ConfigFlagSource
    from pyfly.feature_flags.sources.file import FileFlagSource

# The module of each public name.
_EXPORTS: dict[str, str] = {
    "FeatureFlags": "pyfly.feature_flags.client",
    "OpenFeatureBinding": "pyfly.feature_flags.client",
    "ComposedFlag": "pyfly.feature_flags.composition",
    "Composition": "pyfly.feature_flags.composition",
    "Layer": "pyfly.feature_flags.composition",
    "compose": "pyfly.feature_flags.composition",
    "ApplicationContextContributor": "pyfly.feature_flags.context",
    "EvaluationContextContributor": "pyfly.feature_flags.context",
    "EvaluationContextResolver": "pyfly.feature_flags.context",
    "FeatureFlagsContextFilter": "pyfly.feature_flags.context",
    "SecurityContextContributor": "pyfly.feature_flags.context",
    "TenantContextContributor": "pyfly.feature_flags.context",
    "FlagDefinitionError": "pyfly.feature_flags.definitions",
    "FlagDocument": "pyfly.feature_flags.definitions",
    "normalize_flags": "pyfly.feature_flags.definitions",
    "parse_document": "pyfly.feature_flags.definitions",
    "validate_flag": "pyfly.feature_flags.definitions",
    "FeatureFlagEvaluated": "pyfly.feature_flags.events",
    "FeatureFlagUpdated": "pyfly.feature_flags.events",
    "FeatureFlagsChanged": "pyfly.feature_flags.events",
    "ExposureEventHook": "pyfly.feature_flags.hooks",
    "MetricsHook": "pyfly.feature_flags.hooks",
    "FeatureFlagsProperties": "pyfly.feature_flags.properties",
    "FireflyFlagProvider": "pyfly.feature_flags.provider",
    "FeatureFlagsError": "pyfly.feature_flags.registry",
    "FlagRegistry": "pyfly.feature_flags.registry",
    "SourceStatus": "pyfly.feature_flags.registry",
    "install_feature_flags": "pyfly.feature_flags.slot",
    "installed_feature_flags": "pyfly.feature_flags.slot",
    "uninstall_feature_flags": "pyfly.feature_flags.slot",
    "FlagSource": "pyfly.feature_flags.sources",
    "FlagSourceError": "pyfly.feature_flags.sources",
    "SourceSnapshot": "pyfly.feature_flags.sources",
    "ConfigFlagSource": "pyfly.feature_flags.sources.config",
    "FileFlagSource": "pyfly.feature_flags.sources.file",
}

__all__ = [
    "ApplicationContextContributor",
    "ComposedFlag",
    "Composition",
    "ConfigFlagSource",
    "EvaluationContextContributor",
    "EvaluationContextResolver",
    "ExposureEventHook",
    "FeatureFlagEvaluated",
    "FeatureFlagUpdated",
    "FeatureFlags",
    "FeatureFlagsChanged",
    "FeatureFlagsContextFilter",
    "FeatureFlagsError",
    "FeatureFlagsProperties",
    "FileFlagSource",
    "FireflyFlagProvider",
    "FlagDefinitionError",
    "FlagDocument",
    "FlagRegistry",
    "FlagSource",
    "FlagSourceError",
    "Layer",
    "MetricsHook",
    "OpenFeatureBinding",
    "SecurityContextContributor",
    "SourceSnapshot",
    "SourceStatus",
    "TenantContextContributor",
    "compose",
    "install_feature_flags",
    "installed_feature_flags",
    "normalize_flags",
    "parse_document",
    "uninstall_feature_flags",
    "validate_flag",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
