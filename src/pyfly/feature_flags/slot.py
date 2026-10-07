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
"""The installed facade: how ``@feature_flag`` and the test support reach the running application's flags.

``OpenFeatureBinding`` installs it when the context starts and removes it when it stops, only if it is still its
own (a later context may have replaced it) — the ``@transactional`` precedent
(``pyfly.data.transaction.registry.install_registry``). No OpenFeature import here: gating works without the extra.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyfly.feature_flags.client import FeatureFlags

__all__ = ["InstalledFlags", "install_feature_flags", "installed_feature_flags", "uninstall_feature_flags"]


@dataclass(frozen=True)
class InstalledFlags:
    """The application's facade, and the status a disabled gate answers (``web.disabled-status``)."""

    facade: FeatureFlags
    disabled_status: int = 404


_lock = threading.Lock()
_installed: InstalledFlags | None = None


def install_feature_flags(facade: FeatureFlags, *, disabled_status: int = 404) -> None:
    """Make *facade* the one gates evaluate with (the context's start does it)."""
    global _installed
    with _lock:
        _installed = InstalledFlags(facade, disabled_status)


def uninstall_feature_flags(facade: FeatureFlags) -> None:
    """Remove *facade*, only if it is still the installed one (a later context may have replaced it)."""
    global _installed
    with _lock:
        if _installed is not None and _installed.facade is facade:
            _installed = None


def installed_feature_flags() -> InstalledFlags | None:
    """The installed facade, or ``None`` outside a started application context."""
    return _installed
