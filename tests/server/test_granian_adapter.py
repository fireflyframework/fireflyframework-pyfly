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
"""Tests for GranianServerAdapter."""

from __future__ import annotations

from importlib.util import find_spec
from unittest.mock import patch

import pytest

from pyfly.server.ports.outbound import ApplicationServerPort
from pyfly.server.types import ServerInfo

HAS_GRANIAN = find_spec("granian") is not None
pytestmark = pytest.mark.skipif(not HAS_GRANIAN, reason="granian not installed")


class TestGranianServerAdapter:
    def test_is_application_server_port(self):
        from pyfly.server.adapters.granian.adapter import GranianServerAdapter

        adapter = GranianServerAdapter()
        assert isinstance(adapter, ApplicationServerPort)

    def test_server_info(self):
        from pyfly.server.adapters.granian.adapter import GranianServerAdapter

        adapter = GranianServerAdapter()
        info = adapter.server_info
        assert isinstance(info, ServerInfo)
        assert info.name == "granian"

    @patch("granian.Granian")
    def test_serve_creates_granian_with_config(self, mock_granian_cls):
        from pyfly.config.properties.server import ServerProperties
        from pyfly.server.adapters.granian.adapter import GranianServerAdapter

        adapter = GranianServerAdapter()
        config = ServerProperties(type="granian", workers=2)
        # Need to give config host/port attrs that the adapter reads
        config.host = "0.0.0.0"
        config.port = 8000
        adapter.serve("myapp:app", config)

        mock_granian_cls.assert_called_once()
        call_kwargs = mock_granian_cls.call_args[1]
        assert call_kwargs["target"] == "myapp:app"
        assert call_kwargs["workers"] == 2
        mock_granian_cls.return_value.serve.assert_called_once()

    def test_shutdown_does_not_raise(self):
        from pyfly.server.adapters.granian.adapter import GranianServerAdapter

        adapter = GranianServerAdapter()
        adapter.shutdown()
