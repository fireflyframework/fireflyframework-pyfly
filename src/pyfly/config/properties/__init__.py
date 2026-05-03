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
"""Typed configuration property classes for each PyFly subsystem."""

from pyfly.config.properties.cache import CacheProperties
from pyfly.config.properties.client import ClientProperties
from pyfly.config.properties.data import RelationalProperties
from pyfly.config.properties.logging import LoggingProperties
from pyfly.config.properties.messaging import MessagingProperties
from pyfly.config.properties.mongodb import DocumentProperties
from pyfly.config.properties.server import GranianProperties, ServerProperties
from pyfly.config.properties.web import WebProperties

__all__ = [
    "CacheProperties",
    "ClientProperties",
    "DocumentProperties",
    "GranianProperties",
    "LoggingProperties",
    "MessagingProperties",
    "RelationalProperties",
    "ServerProperties",
    "WebProperties",
]
