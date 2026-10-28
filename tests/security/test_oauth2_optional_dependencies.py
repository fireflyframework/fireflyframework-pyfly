# Copyright 2026 Firefly Software Foundation.
# SPDX-License-Identifier: Apache-2.0
"""Headless OAuth services do not require an HTTP adapter installation."""

import os
import subprocess
import sys
from pathlib import Path


def test_headless_oauth_imports_without_optional_http_packages() -> None:
    script = """
import importlib.abc
import sys
class NoHttpPackages(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'starlette', 'fastapi', 'httpx'}:
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, NoHttpPackages())
from pyfly.security.oauth2 import AuthorizationServer, InMemoryTokenStore, ClientRegistration
from pyfly.security.oauth2 import JWKSTokenValidator
assert AuthorizationServer and InMemoryTokenStore and ClientRegistration and JWKSTokenValidator
assert not {'starlette', 'fastapi', 'httpx'} & sys.modules.keys()
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_lazy_http_exports_keep_their_public_type_identity() -> None:
    import pyfly.security.oauth2 as oauth
    from pyfly.security.oauth2.endpoints import AuthorizationServerEndpoints
    from pyfly.security.oauth2.login import OAuth2LoginHandler

    assert oauth.AuthorizationServerEndpoints is AuthorizationServerEndpoints
    assert oauth.OAuth2LoginHandler is OAuth2LoginHandler
