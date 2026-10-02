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
"""The public names of pyfly.feature_flags, and the package without the feature-flags extra (Review focus 4)."""

from __future__ import annotations

import subprocess
import sys

import pytest

import pyfly.feature_flags
from pyfly.feature_flags import registry

BLOCK_OPENFEATURE = "import sys; sys.modules['openfeature'] = None; "


def test_every_public_name_resolves() -> None:
    for name in pyfly.feature_flags.__all__:
        assert getattr(pyfly.feature_flags, name) is not None, name
    assert set(pyfly.feature_flags.__all__) <= set(dir(pyfly.feature_flags))


def test_every_lazy_export_is_public() -> None:
    """``__all__`` and the lazy-export table name the same names (a later lane edits both)."""
    assert sorted(pyfly.feature_flags._EXPORTS) == sorted(pyfly.feature_flags.__all__)
    assert pyfly.feature_flags.__all__ == sorted(pyfly.feature_flags.__all__)


def test_the_registry_error_is_public() -> None:
    assert "FeatureFlagsError" in pyfly.feature_flags.__all__
    assert pyfly.feature_flags.FeatureFlagsError is registry.FeatureFlagsError


def test_an_unknown_name_is_an_attribute_error() -> None:
    with pytest.raises(AttributeError, match="has no attribute 'NoSuchName'"):
        getattr(pyfly.feature_flags, "NoSuchName")  # noqa: B009


def test_the_package_imports_without_openfeature() -> None:
    """Review focus 4: `import openfeature` raises ImportError when sys.modules holds None for it."""
    code = BLOCK_OPENFEATURE + (
        "import pyfly.feature_flags, pyfly.feature_flags.definitions, pyfly.feature_flags.composition, "
        "pyfly.feature_flags.events, pyfly.feature_flags.slot, pyfly.feature_flags.registry, "
        "pyfly.feature_flags.properties, pyfly.feature_flags.sources.config, pyfly.feature_flags.sources.file; "
        "from pyfly.feature_flags import FeatureFlagsError, FeatureFlagsProperties, FlagRegistry, ConfigFlagSource; "
        "print('ok')"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_without_openfeature_the_auto_configuration_is_skipped() -> None:
    code = BLOCK_OPENFEATURE + (
        "from pyfly.config.auto import discover_auto_configurations; "
        "print(sorted(c.__name__ for c in discover_auto_configurations() if 'Feature' in c.__name__))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
