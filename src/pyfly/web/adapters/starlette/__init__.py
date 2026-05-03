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
"""Starlette web framework adapter — default WebServerPort implementation."""

from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.adapters.starlette.controller import ControllerRegistrar
from pyfly.web.adapters.starlette.docs import (
    make_openapi_endpoint,
    make_redoc_endpoint,
    make_swagger_ui_endpoint,
)
from pyfly.web.adapters.starlette.errors import global_exception_handler
from pyfly.web.adapters.starlette.filter_chain import WebFilterChainMiddleware
from pyfly.web.adapters.starlette.filters import (
    RequestLoggingFilter,
    SecurityHeadersFilter,
    TransactionIdFilter,
)
from pyfly.web.adapters.starlette.request_logger import RequestLoggingMiddleware
from pyfly.web.adapters.starlette.resolver import ParameterResolver
from pyfly.web.adapters.starlette.response import handle_return_value
from pyfly.web.adapters.starlette.security_headers import SecurityHeadersMiddleware

__all__ = [
    "ControllerRegistrar",
    "ParameterResolver",
    "RequestLoggingFilter",
    "RequestLoggingMiddleware",
    "SecurityHeadersFilter",
    "SecurityHeadersMiddleware",
    "TransactionIdFilter",
    "WebFilterChainMiddleware",
    "create_app",
    "global_exception_handler",
    "handle_return_value",
    "make_openapi_endpoint",
    "make_redoc_endpoint",
    "make_swagger_ui_endpoint",
]

try:
    from pyfly.web.adapters.starlette.filters import SecurityFilter

    __all__ += ["SecurityFilter"]
except ImportError:
    pass

try:
    from pyfly.web.adapters.starlette.filters import OAuth2ResourceServerFilter

    __all__ += ["OAuth2ResourceServerFilter"]
except ImportError:
    pass
