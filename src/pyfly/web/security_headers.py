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
"""Security headers configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SecurityHeadersConfig:
    """Configuration for security response headers.

    All headers enabled by default following OWASP recommendations.
    """

    x_content_type_options: str = "nosniff"
    x_frame_options: str = "DENY"
    strict_transport_security: str = "max-age=31536000; includeSubDomains"
    x_xss_protection: str = "0"  # Modern browsers: disable legacy XSS auditor
    referrer_policy: str = "strict-origin-when-cross-origin"
    content_security_policy: str | None = None  # None = don't add (too app-specific)
    permissions_policy: str | None = None  # None = don't add
