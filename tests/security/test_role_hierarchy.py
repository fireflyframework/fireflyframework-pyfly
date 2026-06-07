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
"""Role hierarchy (v26.06.45): higher roles imply lower ones in method-security checks."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from pyfly.security import RoleHierarchy, get_role_hierarchy, set_role_hierarchy
from pyfly.security.context import SecurityContext
from pyfly.security.expression import evaluate_security_expression as ev


@pytest.fixture(autouse=True)
def _reset_hierarchy() -> Iterator[None]:
    yield
    set_role_hierarchy(None)  # module-global must not leak into other tests


def test_expand_transitive() -> None:
    h = RoleHierarchy.from_string("ADMIN > MANAGER\nMANAGER > USER")
    assert h.expand(["ADMIN"]) == {"ADMIN", "MANAGER", "USER"}
    assert h.expand(["MANAGER"]) == {"MANAGER", "USER"}
    assert h.expand(["USER"]) == {"USER"}
    assert h.expand([]) == set()


def test_from_string_separators_and_noise() -> None:
    h = RoleHierarchy.from_string("ADMIN > USER ; USER > GUEST\n\nnonsense-without-arrow")
    assert h.expand(["ADMIN"]) == {"ADMIN", "USER", "GUEST"}


def test_hierarchy_makes_admin_satisfy_lower_roles() -> None:
    admin = SecurityContext(user_id="u", roles=["ADMIN"])
    # Without a hierarchy, ADMIN is not implicitly USER (back-compat).
    assert ev("hasRole('USER')", admin) is False

    set_role_hierarchy(RoleHierarchy.from_string("ADMIN > USER"))
    assert get_role_hierarchy() is not None
    assert ev("hasRole('USER')", admin) is True
    assert ev("hasAnyRole('USER')", admin) is True
    assert ev("hasAuthority('USER')", admin) is True


def test_hierarchy_does_not_grant_unrelated_roles() -> None:
    admin = SecurityContext(user_id="u", roles=["ADMIN"])
    set_role_hierarchy(RoleHierarchy.from_string("ADMIN > USER"))
    assert ev("hasRole('SUPERUSER')", admin) is False
    assert ev("hasRole('ADMIN')", admin) is True  # still has its own role
