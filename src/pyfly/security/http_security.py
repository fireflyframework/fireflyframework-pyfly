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
"""URL-level security DSL — builder for filter-based authorization rules.

Provides a Spring-inspired ``HttpSecurity`` builder that configures URL-pattern
authorization rules evaluated by :class:`HttpSecurityFilter` *before* the route
handler is reached.

Usage::

    http_security = HttpSecurity()
    http_security.authorize_requests() \\
        .request_matchers("/api/admin/**").has_role("ADMIN") \\
        .request_matchers("/api/**").authenticated() \\
        .request_matchers("/health", "/docs", "/openapi.json").permit_all() \\
        .any_request().deny_all()

    http_security_filter = http_security.build()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyfly.web.adapters.starlette.filters.http_security_filter import HttpSecurityFilter


# ---------------------------------------------------------------------------
# Access rule model
# ---------------------------------------------------------------------------


class AccessRuleType(Enum):
    """The kind of access check to perform on a matched URL pattern."""

    PERMIT_ALL = auto()
    DENY_ALL = auto()
    AUTHENTICATED = auto()
    HAS_ROLE = auto()
    HAS_ANY_ROLE = auto()
    HAS_PERMISSION = auto()


@dataclass(frozen=True)
class AccessRule:
    """A single authorization rule applied to one or more URL patterns.

    Attributes:
        rule_type: The kind of check to perform.
        value: The role name, permission string, or list of role names,
            depending on ``rule_type``.  ``None`` for rules that need no
            additional data (PERMIT_ALL, DENY_ALL, AUTHENTICATED).
    """

    rule_type: AccessRuleType
    value: str | list[str] | None = None


@dataclass
class SecurityRule:
    """A pairing of URL patterns and the access rule that guards them.

    Attributes:
        patterns: Glob patterns (fnmatch-style) to match against the
            request path.  An empty list means "any request".
        rule: The access rule to enforce when a pattern matches.
        methods: Upper-case HTTP methods this rule applies to.  An empty list
            (the default) matches any method.
    """

    patterns: list[str]
    rule: AccessRule
    methods: list[str] = field(default_factory=list)


def _normalize_methods(methods: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Coerce a method spec (str / list / None) into a list of upper-case methods."""
    if methods is None:
        return []
    if isinstance(methods, str):
        return [methods.upper()]
    return [m.upper() for m in methods]


# ---------------------------------------------------------------------------
# Builder DSL
# ---------------------------------------------------------------------------


class _RequestMatcherBuilder:
    """Intermediate builder returned by ``authorize_requests().request_matchers(...)``."""

    def __init__(
        self,
        registry: _AuthorizeRequestsBuilder,
        patterns: list[str],
        methods: list[str] | None = None,
    ) -> None:
        self._registry = registry
        self._patterns = patterns
        self._methods = methods or []

    # -- terminal access-rule methods --

    def permit_all(self) -> _AuthorizeRequestsBuilder:
        """Allow all requests matching the current patterns."""
        self._registry._add_rule(self._patterns, AccessRule(AccessRuleType.PERMIT_ALL), self._methods)
        return self._registry

    def deny_all(self) -> _AuthorizeRequestsBuilder:
        """Deny all requests matching the current patterns."""
        self._registry._add_rule(self._patterns, AccessRule(AccessRuleType.DENY_ALL), self._methods)
        return self._registry

    def authenticated(self) -> _AuthorizeRequestsBuilder:
        """Require an authenticated user for the current patterns."""
        self._registry._add_rule(self._patterns, AccessRule(AccessRuleType.AUTHENTICATED), self._methods)
        return self._registry

    def has_role(self, role: str) -> _AuthorizeRequestsBuilder:
        """Require the user to have *role* for the current patterns."""
        self._registry._add_rule(self._patterns, AccessRule(AccessRuleType.HAS_ROLE, role), self._methods)
        return self._registry

    def has_any_role(self, roles: list[str]) -> _AuthorizeRequestsBuilder:
        """Require the user to have at least one of *roles* for the current patterns."""
        self._registry._add_rule(self._patterns, AccessRule(AccessRuleType.HAS_ANY_ROLE, list(roles)), self._methods)
        return self._registry

    def has_permission(self, permission: str) -> _AuthorizeRequestsBuilder:
        """Require the user to have *permission* for the current patterns."""
        self._registry._add_rule(self._patterns, AccessRule(AccessRuleType.HAS_PERMISSION, permission), self._methods)
        return self._registry


class _AuthorizeRequestsBuilder:
    """Fluent builder for accumulating URL-pattern authorization rules.

    Returned by :meth:`HttpSecurity.authorize_requests`.
    """

    def __init__(self, security: HttpSecurity) -> None:
        self._security = security

    def request_matchers(
        self, *patterns: str, methods: str | list[str] | tuple[str, ...] | None = None
    ) -> _RequestMatcherBuilder:
        """Begin a rule for one or more URL glob patterns.

        Args:
            *patterns: fnmatch-style glob patterns (e.g. ``"/api/admin/**"``).
            methods: Optional HTTP method(s) the rule applies to (e.g. ``"POST"``
                or ``["PUT", "DELETE"]``). When omitted, the rule matches any
                method — mirroring Spring's ``requestMatchers(HttpMethod.X, ...)``.

        Returns:
            A :class:`_RequestMatcherBuilder` to set the access rule.
        """
        return _RequestMatcherBuilder(self, list(patterns), _normalize_methods(methods))

    def any_request(self, *, methods: str | list[str] | tuple[str, ...] | None = None) -> _RequestMatcherBuilder:
        """Begin a catch-all rule that matches any request path.

        This should be the **last** rule in the chain. An optional ``methods``
        restricts the catch-all to specific HTTP methods.
        """
        return _RequestMatcherBuilder(self, [], _normalize_methods(methods))

    def _add_rule(self, patterns: list[str], rule: AccessRule, methods: list[str] | None = None) -> None:
        self._security._rules.append(SecurityRule(patterns=patterns, rule=rule, methods=methods or []))


# ---------------------------------------------------------------------------
# Top-level builder
# ---------------------------------------------------------------------------


@dataclass
class HttpSecurity:
    """URL-level security configuration builder.

    Collects authorization rules through a fluent DSL and builds an
    :class:`HttpSecurityFilter` that enforces them at the filter layer.
    """

    _rules: list[SecurityRule] = field(default_factory=list)

    @property
    def rules(self) -> list[SecurityRule]:
        """Return the accumulated security rules (read-only snapshot)."""
        return list(self._rules)

    def authorize_requests(self) -> _AuthorizeRequestsBuilder:
        """Start defining URL-pattern authorization rules.

        Returns:
            An :class:`_AuthorizeRequestsBuilder` for method chaining.
        """
        return _AuthorizeRequestsBuilder(self)

    def build(self) -> HttpSecurityFilter:
        """Create an :class:`HttpSecurityFilter` configured with the accumulated rules.

        The filter is ordered at ``HIGHEST_PRECEDENCE + 350`` so it runs
        **after** authentication filters and **before** the route handler.
        """
        from pyfly.web.adapters.starlette.filters.http_security_filter import HttpSecurityFilter

        # Unmatched requests are denied by default (fail-closed) by the filter when
        # any rules are configured — add ``.any_request().permit_all()`` (or list
        # public paths) to allow them.
        return HttpSecurityFilter(rules=list(self._rules))
