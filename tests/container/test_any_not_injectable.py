"""Regression: ``Any`` / ``Any | None`` parameters are NOT injectable.

A parameter typed ``Any`` (or ``Any | None``) must fall back to its default
instead of resolving to whatever bean happens to be registered under ``Any``
(e.g. an ``@bean ... -> Any``). Injecting the wrong object caused
`'CacheHealthIndicator' object has no attribute 'counter'` at startup when such a
value landed in ``RuleEngineService(metrics: Any | None = None)``.
"""

from typing import Any

import pytest

from pyfly.container.container import Container
from pyfly.container.exceptions import NoSuchBeanError


def test_plain_any_is_not_injectable() -> None:
    with pytest.raises(NoSuchBeanError):
        Container()._resolve_param(Any)


def test_optional_any_resolves_to_none() -> None:
    assert Container()._resolve_param(Any | None) is None
