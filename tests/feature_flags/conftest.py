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
"""Isolation for the feature-flag tests: the OpenFeature API (providers, hooks, propagator), the gating slot and the
bindings' record of superseded providers are process-global."""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _reset_openfeature() -> Iterator[None]:
    yield
    from openfeature import api

    from pyfly.feature_flags import client
    from pyfly.feature_flags.slot import installed_feature_flags, uninstall_feature_flags

    installed = installed_feature_flags()
    if installed is not None:
        uninstall_feature_flags(installed.facade)
    api.shutdown()  # providers -> NoOp, hooks, API evaluation context and propagator cleared
    with client._superseded_lock:  # what a stopped binding recorded for the next one on its domain
        client._superseded.clear()
