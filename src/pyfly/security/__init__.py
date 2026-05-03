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
"""PyFly Security — Authentication, authorization, and JWT integration."""

from pyfly.security.context import SecurityContext
from pyfly.security.decorators import secure
from pyfly.security.http_security import AccessRule, AccessRuleType, HttpSecurity, SecurityRule
from pyfly.security.method_security import post_authorize, pre_authorize
from pyfly.security.middleware import SecurityMiddleware

__all__ = [
    "AccessRule",
    "AccessRuleType",
    "HttpSecurity",
    "SecurityContext",
    "SecurityMiddleware",
    "SecurityRule",
    "post_authorize",
    "pre_authorize",
    "secure",
]

try:
    from pyfly.security.jwt import JWTService

    __all__ += ["JWTService"]
except ImportError:
    pass

try:
    from pyfly.security.password import BcryptPasswordEncoder, PasswordEncoder

    __all__ += ["BcryptPasswordEncoder", "PasswordEncoder"]
except ImportError:
    pass
