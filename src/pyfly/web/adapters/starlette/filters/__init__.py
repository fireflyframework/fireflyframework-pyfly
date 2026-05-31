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
"""Built-in WebFilter implementations for Starlette."""

from pyfly.web.adapters.starlette.filters.correlation_filter import CorrelationFilter
from pyfly.web.adapters.starlette.filters.http_security_filter import HttpSecurityFilter
from pyfly.web.adapters.starlette.filters.request_context_filter import RequestContextFilter
from pyfly.web.adapters.starlette.filters.request_logging_filter import RequestLoggingFilter
from pyfly.web.adapters.starlette.filters.security_headers_filter import SecurityHeadersFilter
from pyfly.web.adapters.starlette.filters.transaction_id_filter import TransactionIdFilter

__all__ = [
    "CorrelationFilter",
    "HttpSecurityFilter",
    "RequestContextFilter",
    "RequestLoggingFilter",
    "SecurityHeadersFilter",
    "TransactionIdFilter",
]

try:
    from pyfly.web.adapters.starlette.filters.security_filter import SecurityFilter

    __all__ += ["SecurityFilter"]
except ImportError:
    pass

try:
    from pyfly.web.adapters.starlette.filters.oauth2_resource_filter import OAuth2ResourceServerFilter

    __all__ += ["OAuth2ResourceServerFilter"]
except ImportError:
    pass

try:
    from pyfly.web.adapters.starlette.filters.metrics_filter import MetricsFilter

    __all__ += ["MetricsFilter"]
except ImportError:
    pass
