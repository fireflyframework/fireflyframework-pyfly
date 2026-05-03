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
"""CSRF token utilities — double-submit cookie pattern.

Provides token generation and timing-safe validation for the
double-submit cookie CSRF protection strategy.
"""

from __future__ import annotations

import secrets

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CSRF_COOKIE_NAME: str = "XSRF-TOKEN"
"""Name of the cookie that carries the CSRF token."""

CSRF_HEADER_NAME: str = "X-XSRF-TOKEN"
"""Name of the request header that carries the CSRF token."""

SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
"""HTTP methods that do not require CSRF validation."""


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------
def generate_csrf_token() -> str:
    """Generate a cryptographically-secure CSRF token.

    Returns:
        A URL-safe base64-encoded random string (43 characters).
    """
    return secrets.token_urlsafe(32)


def validate_csrf_token(cookie_token: str, header_token: str) -> bool:
    """Validate a CSRF token using timing-safe comparison.

    Args:
        cookie_token: The token value from the ``XSRF-TOKEN`` cookie.
        header_token: The token value from the ``X-XSRF-TOKEN`` header.

    Returns:
        ``True`` if both tokens match; ``False`` otherwise.
    """
    return secrets.compare_digest(cookie_token, header_token)
