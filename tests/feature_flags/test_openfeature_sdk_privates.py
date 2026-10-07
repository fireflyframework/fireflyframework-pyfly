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
"""The OpenFeature SDK privates ``pyfly.feature_flags.client`` relies on, and its public fallbacks.

The SDK has no public way to tell an unbound domain from one bound to the default provider, nor to unbind a domain,
so the OpenFeature binding reads and edits the SDK registry. The canary below fails loudly when an
``openfeature-sdk`` upgrade moves any of it; the fallback tests pin what happens if one ships anyway.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import subprocess
import sys
from typing import Any

import pytest
from openfeature import api
from openfeature.client import OpenFeatureClient
from openfeature.provider.no_op_provider import NoOpProvider

import pyfly.feature_flags.client as client_module
from pyfly.feature_flags.client import FeatureFlags, OpenFeatureBinding
from pyfly.feature_flags.context import EvaluationContextResolver
from pyfly.feature_flags.provider import FireflyFlagProvider
from tests.feature_flags.support import bool_flag

_UPGRADE = (
    "openfeature-sdk changed a private that pyfly/feature_flags/client.py relies on ({what}). The OpenFeature binding "
    "now runs on its public fallbacks (a stopped binding leaves a no-op provider bound to its domain instead of "
    "unbinding it). Update _bound_provider/_unbind/_missing_privates in client.py to the new SDK, then this canary."
)


# -- the canary --------------------------------------------------------------------------------------------------


def _registry() -> Any:
    try:
        module = importlib.import_module("openfeature.provider._registry")
    except ImportError as error:  # pragma: no cover - the canary's failure path
        pytest.fail(_UPGRADE.format(what=f"openfeature.provider._registry is not importable: {error}"))
    registry = getattr(module, "provider_registry", None)
    assert registry is not None, _UPGRADE.format(what="openfeature.provider._registry.provider_registry is gone")
    return registry


def test_canary_the_registry_module_and_its_instance_are_importable() -> None:
    registry = _registry()
    assert registry is api.provider_registry, _UPGRADE.format(what="the API no longer uses that registry instance")


def test_canary_the_registry_keeps_its_domain_bindings_in_a_dict() -> None:
    providers = getattr(_registry(), "_providers", None)
    assert isinstance(providers, dict), _UPGRADE.format(
        what=f"provider_registry._providers is {type(providers).__name__}, not the dict of domain bindings"
    )
    api.set_provider_and_wait(NoOpProvider(), "canary")
    assert "canary" in providers, _UPGRADE.format(what="set_provider no longer records a domain in _providers")


def test_canary_the_registry_lock_is_a_context_manager() -> None:
    lock = getattr(_registry(), "_lock", None)
    assert hasattr(lock, "__enter__") and hasattr(lock, "__exit__"), _UPGRADE.format(
        what=f"provider_registry._lock is {type(lock).__name__}, not usable in a with statement"
    )
    with lock:  # re-entrant: the SDK takes it again inside set_provider, the binding takes it alone
        pass


def test_canary_the_registry_shuts_an_unused_provider_down_with_one_argument() -> None:
    shutdown_if_unused = getattr(_registry(), "_shutdown_if_unused", None)
    assert callable(shutdown_if_unused), _UPGRADE.format(what="provider_registry._shutdown_if_unused is gone")
    signature = inspect.signature(shutdown_if_unused)
    try:
        signature.bind(NoOpProvider())  # how client.py calls it: one positional argument, nothing else required
    except TypeError:
        pytest.fail(_UPGRADE.format(what=f"provider_registry._shutdown_if_unused takes {signature}, not (provider)"))


def test_canary_the_binding_sees_every_private_it_relies_on() -> None:
    assert client_module._missing_privates() == [], _UPGRADE.format(what=client_module._missing_privates())


# -- the public fallbacks ------------------------------------------------------------------------------------------


class _Hiding:
    """The real registry with one private hidden, as an SDK that renamed it would look to the binding."""

    def __init__(self, registry: Any, hidden: str) -> None:
        self._registry = registry
        self._hidden = hidden

    def __getattr__(self, name: str) -> Any:
        if name == self._hidden:
            raise AttributeError(name)
        return getattr(self._registry, name)


def _binding(domain: str) -> OpenFeatureBinding:
    provider = FireflyFlagProvider()
    provider.update({"flags": {"a": bool_flag()}})
    facade = FeatureFlags(OpenFeatureClient(domain=domain, version=None), EvaluationContextResolver())
    return OpenFeatureBinding(provider, facade, domain=domain)


def _fallback_warnings(caplog: pytest.LogCaptureFixture) -> list[Any]:
    return [
        getattr(record, "missing", None)
        for record in caplog.records
        if record.name == "pyfly.feature_flags.client" and record.getMessage() == "feature_flags_openfeature_fallback"
    ]


@pytest.fixture
def _not_warned_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback warning is logged once per process: each test starts before it."""
    monkeypatch.setattr(client_module, "_fallback_warned", False)


@pytest.mark.usefixtures("_not_warned_yet")
async def test_without_the_registry_the_binding_falls_back_and_warns_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The registry module relocated: the binding still installs and stops, on the public API only."""
    monkeypatch.setattr(client_module, "_provider_registry", None)
    with caplog.at_level(logging.WARNING, logger="pyfly.feature_flags.client"):
        for _ in range(2):
            binding = _binding("relocated")
            await binding.start()
            assert binding._facade.is_enabled("a") is True  # noqa: SLF001
            await binding.stop()
            # the closest public behavior: the domain is bound to a no-op provider, not unbound
            assert isinstance(OpenFeatureClient(domain="relocated", version=None).provider, NoOpProvider)
    assert _fallback_warnings(caplog) == [["openfeature.provider._registry.provider_registry"]]  # once per process


@pytest.mark.usefixtures("_not_warned_yet")
async def test_a_hidden_domain_map_engages_the_fallbacks_and_is_named(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(client_module, "_provider_registry", _Hiding(api.provider_registry, "_providers"))
    binding = _binding("hidden-map")
    with caplog.at_level(logging.WARNING, logger="pyfly.feature_flags.client"):
        await binding.start()
        await binding.stop()
    assert _fallback_warnings(caplog) == [["provider_registry._providers"]]
    assert isinstance(api.provider_registry._providers.get("hidden-map"), NoOpProvider)  # noqa: SLF001


@pytest.mark.usefixtures("_not_warned_yet")
async def test_a_hidden_shutdown_still_unbinds_and_is_named(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Without ``_shutdown_if_unused`` the domain is still unbound; only the SDK's shutdown of the unused provider is
    skipped (Firefly's provider keeps its document on shutdown anyway)."""
    monkeypatch.setattr(client_module, "_provider_registry", _Hiding(api.provider_registry, "_shutdown_if_unused"))
    binding = _binding("hidden-shutdown")
    with caplog.at_level(logging.WARNING, logger="pyfly.feature_flags.client"):
        await binding.start()
        await binding.stop()
    assert _fallback_warnings(caplog) == [["provider_registry._shutdown_if_unused"]]
    assert "hidden-shutdown" not in api.provider_registry._providers  # noqa: SLF001


def test_a_relocated_registry_module_does_not_break_the_import() -> None:
    """``openfeature.api`` is loaded first (a relocated module would be imported from its new place), then the old
    module path disappears: ``pyfly.feature_flags.client`` must still import and find every private missing."""
    code = (
        "import sys, openfeature.api, openfeature.provider; "
        "sys.modules['openfeature.provider._registry'] = None; "
        "delattr(openfeature.provider, '_registry'); "
        "import pyfly.feature_flags.client as client; "
        "print(client._missing_privates())"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "['openfeature.provider._registry.provider_registry']"
