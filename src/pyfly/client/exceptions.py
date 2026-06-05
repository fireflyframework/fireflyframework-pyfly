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
"""Service-client exception hierarchy and HTTP status → exception mapping.

Mirrors the Java ``HttpErrorMapper`` so a declarative client surfaces 4xx/5xx
responses as typed exceptions (with request context) instead of silently
returning the error payload as if it were a success (audit #12). Exceptions
flagged ``retryable`` are the only ones the client retry policy retries (#13).
"""

from __future__ import annotations

from typing import Any

from pyfly.kernel.exceptions import InfrastructureException


class ServiceClientException(InfrastructureException):
    """Base for all declarative/REST service-client errors."""

    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        method: str | None = None,
        url: str | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.method = method
        self.url = url
        self.body = body


class ServiceValidationException(ServiceClientException):
    """HTTP 400 — the request was rejected as invalid."""


class ServiceAuthenticationException(ServiceClientException):
    """HTTP 401 / 403 — authentication or authorization failed."""


class ServiceNotFoundException(ServiceClientException):
    """HTTP 404 — the target resource does not exist."""


class ServiceConflictException(ServiceClientException):
    """HTTP 409 — the request conflicts with the current resource state."""


class ServiceUnprocessableEntityException(ServiceClientException):
    """HTTP 422 — the request was well-formed but semantically invalid."""


class ServiceRateLimitException(ServiceClientException):
    """HTTP 429 — the client has been rate limited (retryable)."""

    retryable = True


class ServiceUnavailableException(ServiceClientException):
    """HTTP 5xx — the upstream service failed transiently (retryable)."""

    retryable = True


def map_http_error(
    status_code: int,
    *,
    method: str | None = None,
    url: str | None = None,
    body: Any = None,
) -> ServiceClientException:
    """Build the typed exception for an error HTTP *status_code*."""
    mapping: dict[int, type[ServiceClientException]] = {
        400: ServiceValidationException,
        401: ServiceAuthenticationException,
        403: ServiceAuthenticationException,
        404: ServiceNotFoundException,
        409: ServiceConflictException,
        422: ServiceUnprocessableEntityException,
        429: ServiceRateLimitException,
    }
    exc_type = mapping.get(status_code)
    if exc_type is None:
        exc_type = ServiceUnavailableException if status_code >= 500 else ServiceClientException
    return exc_type(
        f"HTTP {status_code} from {method or 'request'} {url or ''}".strip(),
        status_code=status_code,
        method=method,
        url=url,
        body=body,
    )
