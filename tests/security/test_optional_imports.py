# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Regression test: ``pyfly.security`` must import without infra extras.

Before v26.05.04 ``pyfly.security.__init__`` unconditionally re-exported
``SecurityMiddleware``, which transitively imported ``starlette`` and
``jwt`` at module load time. That made ``import pyfly`` itself fail
when those optional packages were missing — even for non-HTTP services
that just want the kernel + DDD primitives.

The fix wraps the ``SecurityMiddleware`` import in ``try / except
ImportError`` so it's only exposed when its dependencies are
satisfied. This test pins the behaviour by checking that the
package's exports degrade gracefully.
"""

from __future__ import annotations

import importlib

import pytest


def test_pyfly_security_imports_without_starlette_or_jwt() -> None:
    """The package must import even if ``starlette`` / ``jwt`` are missing.

    We can't easily uninstall packages mid-test, so this test merely
    verifies that the ``__init__`` doesn't *unconditionally* depend on
    them — the fix is to check that ``SecurityMiddleware`` is wrapped
    in a ``try / except`` import block, and that the package can be
    fully reloaded without crashing.
    """
    import pyfly.security as sec

    # Reload to exercise the fresh import path again.
    importlib.reload(sec)

    # The unconditional symbols must always be available.
    for name in (
        "AccessRule",
        "AccessRuleType",
        "HttpSecurity",
        "SecurityContext",
        "SecurityRule",
        "post_authorize",
        "pre_authorize",
        "secure",
    ):
        assert hasattr(sec, name), f"missing required symbol: {name}"


def test_security_middleware_import_is_optional() -> None:
    """Confirm the import is guarded — same source-shape used by
    JWTService and BcryptPasswordEncoder, our reference patterns.
    """
    spec = importlib.util.find_spec("pyfly.security")
    assert spec is not None
    src = spec.origin
    assert src is not None
    with open(src) as fh:
        body = fh.read()

    # The eager import on the canonical symbol must be wrapped in a
    # ``try`` block (matching the JWTService / password helpers).
    assert "from pyfly.security.middleware import SecurityMiddleware" in body
    middleware_idx = body.index("from pyfly.security.middleware import SecurityMiddleware")
    preceding_try = body.rfind("try:", 0, middleware_idx)
    assert preceding_try != -1, (
        "SecurityMiddleware import must be wrapped in a try / except ImportError "
        "block so the package stays importable without starlette/pyjwt."
    )
    # The matching except clause must appear after the import:
    after = body[middleware_idx:]
    assert "except ImportError" in after, "missing except ImportError after SecurityMiddleware import"


@pytest.mark.parametrize(
    "extra_symbol",
    ["JWTService", "BcryptPasswordEncoder", "PasswordEncoder", "SecurityMiddleware"],
)
def test_optional_security_symbols_are_in_all_when_available(extra_symbol: str) -> None:
    import pyfly.security as sec

    if hasattr(sec, extra_symbol):
        assert extra_symbol in sec.__all__
