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
"""PyFly Security — Authentication, authorization, and JWT integration.

The starlette-specific :class:`SecurityMiddleware`, :class:`JWTService`
and :class:`BcryptPasswordEncoder` are *optional* — they are only
exported when their underlying third-party dependency
(``starlette`` / ``pyjwt`` / ``bcrypt``) is installed. This keeps the
security package importable from non-HTTP applications (workers, CLI
tools, data jobs) that don't pull in the ``[web]`` / ``[security]``
extras.
"""

from pyfly.security.context import SecurityContext
from pyfly.security.decorators import secure
from pyfly.security.expression import get_role_hierarchy, set_role_hierarchy
from pyfly.security.http_security import AccessRule, AccessRuleType, HttpSecurity, SecurityRule
from pyfly.security.method_security import post_authorize, pre_authorize
from pyfly.security.role_hierarchy import RoleHierarchy

__all__ = [
    "AccessRule",
    "AccessRuleType",
    "HttpSecurity",
    "RoleHierarchy",
    "SecurityContext",
    "SecurityRule",
    "get_role_hierarchy",
    "post_authorize",
    "pre_authorize",
    "secure",
    "set_role_hierarchy",
]

try:
    from pyfly.security.middleware import SecurityMiddleware

    __all__ += ["SecurityMiddleware"]
except ImportError:
    # ``pyfly.security.middleware`` re-exports a starlette adapter that
    # transitively requires ``pyjwt``; skip it when those extras aren't
    # installed.
    pass

try:
    from pyfly.security.jwt import JWTService

    __all__ += ["JWTService"]
except ImportError:
    pass

try:
    from pyfly.security.password import (
        Argon2PasswordEncoder,
        BcryptPasswordEncoder,
        DelegatingPasswordEncoder,
        PasswordEncoder,
        Pbkdf2PasswordEncoder,
        ScryptPasswordEncoder,
        create_delegating_password_encoder,
    )

    __all__ += [
        "Argon2PasswordEncoder",
        "BcryptPasswordEncoder",
        "DelegatingPasswordEncoder",
        "PasswordEncoder",
        "Pbkdf2PasswordEncoder",
        "ScryptPasswordEncoder",
        "create_delegating_password_encoder",
    ]
except ImportError:
    pass

from pyfly.security.user_details import (
    InMemoryUserDetailsService,
    UserDetails,
    UserDetailsService,
)

__all__ += ["InMemoryUserDetailsService", "UserDetails", "UserDetailsService"]
