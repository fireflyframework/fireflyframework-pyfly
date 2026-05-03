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
"""Tests for LoggingPort protocol."""

from typing import Any, runtime_checkable

from pyfly.logging.port import LoggingPort


class TestLoggingPortProtocol:
    def test_is_runtime_checkable(self):
        assert hasattr(LoggingPort, "__protocol_attrs__") or runtime_checkable

    def test_conforming_class_is_instance(self):
        class FakeLogging:
            def configure(self, config: Any) -> None:
                pass

            def get_logger(self, name: str) -> Any:
                pass

            def set_level(self, name: str, level: str) -> None:
                pass

        assert isinstance(FakeLogging(), LoggingPort)

    def test_non_conforming_class_is_not_instance(self):
        class Incomplete:
            def get_logger(self, name: str) -> Any:
                pass

        assert not isinstance(Incomplete(), LoggingPort)
