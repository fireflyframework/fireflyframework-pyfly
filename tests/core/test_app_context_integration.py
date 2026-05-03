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
"""Tests for PyFlyApplication integration with ApplicationContext."""

import pytest

from pyfly.context.application_context import ApplicationContext
from pyfly.core.application import PyFlyApplication, pyfly_application


class TestPyFlyApplicationContext:
    @pytest.mark.asyncio
    async def test_application_has_context(self):
        @pyfly_application(name="test-app")
        class TestApp:
            pass

        app = PyFlyApplication(TestApp)
        assert isinstance(app.context, ApplicationContext)

    @pytest.mark.asyncio
    async def test_startup_starts_context(self):
        @pyfly_application(name="test-app")
        class TestApp:
            pass

        app = PyFlyApplication(TestApp)
        await app.startup()
        assert app.context._started is True
        await app.shutdown()
