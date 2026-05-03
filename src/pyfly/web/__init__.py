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
"""PyFly Web — Enterprise web layer with pluggable adapters.

Framework-agnostic types (mappings, params, etc.) are exported directly.
Import concrete adapter types from the adapter package::

    from pyfly.web.adapters.starlette import create_app
"""

# Re-export controller_advice from container for convenience
from pyfly.container.stereotypes import controller_advice
from pyfly.web.cors import CORSConfig
from pyfly.web.exception_handler import exception_handler
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.mappings import (
    delete_mapping,
    get_mapping,
    patch_mapping,
    post_mapping,
    put_mapping,
    request_mapping,
)
from pyfly.web.params import Body, Cookie, File, Header, PathVar, QueryParam, UploadedFile, Valid
from pyfly.web.ports.filter import WebFilter
from pyfly.web.security_headers import SecurityHeadersConfig
from pyfly.web.sse import sse_mapping

__all__ = [
    "Body",
    "CORSConfig",
    "controller_advice",
    "Cookie",
    "File",
    "Header",
    "OncePerRequestFilter",
    "PathVar",
    "QueryParam",
    "SecurityHeadersConfig",
    "UploadedFile",
    "Valid",
    "WebFilter",
    "delete_mapping",
    "exception_handler",
    "get_mapping",
    "patch_mapping",
    "post_mapping",
    "put_mapping",
    "request_mapping",
    "sse_mapping",
]
