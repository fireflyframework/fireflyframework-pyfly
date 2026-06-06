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
"""Regression: pyfly.testing is importable WITHOUT jsonpath-ng (v26.06.20).

jsonpath-ng is an optional (``pyfly[testing]``) dependency used only by
``TestResponse.assert_json_path``. It must be imported lazily so a consumer that
followed a testing-* skill (``from pyfly.testing import ...``) without installing
the extra does not hit ModuleNotFoundError.
"""

from __future__ import annotations

import subprocess
import sys


def test_pyfly_testing_importable_without_jsonpath() -> None:
    # Simulate jsonpath-ng being absent (sys.modules[name] = None makes its import fail),
    # then import the public helpers — must succeed in a clean subprocess.
    code = (
        "import sys; sys.modules['jsonpath_ng'] = None; "
        "from pyfly.testing import (PyFlyTestClient, PyFlyTestCase, mock_bean, "
        "assert_event_published, assert_no_events_published); print('ok')"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
