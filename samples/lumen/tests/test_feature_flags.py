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
"""The wallet's optional offer exercises the real feature gate and provider."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from lumen.app import LumenApplication
from lumen.web.controllers.wallet_controller import WalletController
from sqlalchemy.ext.asyncio import create_async_engine

from pyfly.container import component
from pyfly.context.application_context import ApplicationContext
from pyfly.context.events import app_event_listener
from pyfly.core import PyFlyApplication
from pyfly.core.config import Config
from pyfly.data.transaction import infrastructure_unit
from pyfly.feature_flags.client import FeatureFlags
from pyfly.feature_flags.events import FeatureFlagEvaluated
from pyfly.feature_flags.management import FlagManagement
from pyfly.feature_flags.store.ports import FlagConflictError
from pyfly.feature_flags.store.sqlalchemy import SqlAlchemyFlagStore
from pyfly.testing.feature_flags import override_flags
from pyfly.web.adapters.starlette import create_app


@component
class ExposureCollector:
    def __init__(self) -> None:
        self.events: list[FeatureFlagEvaluated] = []
        self.ready = asyncio.Event()

    @app_event_listener
    async def on_evaluation(self, event: FeatureFlagEvaluated) -> None:
        self.events.append(event)
        self.ready.set()


@pytest.mark.asyncio
async def test_wallet_offer_route_method_falls_back_and_activates() -> None:
    controller = WalletController(None, None)  # this route does not dispatch commands or queries
    with override_flags({"wallet-offer": False}):
        assert await controller.wallet_offer() == {"offer": "standard"}
    with override_flags({"wallet-offer": True}):
        assert await controller.wallet_offer() == {"offer": "new"}
    with override_flags({}):
        assert await controller.wallet_offer() == {"offer": "standard"}


def test_shared_experiment_fixture_selects_the_same_variant_in_lumen() -> None:
    vectors = json.loads(
        (Path(__file__).resolve().parents[3] / "tests/feature_flags/conformance/firefly-vectors.json").read_text()
    )
    case = next(case for case in vectors["cases"] if case["name"] == "experiment arm for alice")
    with override_flags(case["document"]["flags"]) as flags:
        value = flags.get_string(case["flag"], case["default"], targeting_key=case["targetingKey"])
        assert value == case["expect"]["value"]
        assert flags.variant(case["flag"], targeting_key=case["targetingKey"]) == case["expect"]["variant"]


@pytest.mark.asyncio
async def test_booted_lumen_route_changes_with_the_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYFLY_DATA_RELATIONAL_URL", f"sqlite+aiosqlite:///{tmp_path / 'rollout.db'}")
    config_path = Path(__file__).resolve().parents[1] / "pyfly.yaml"
    application = PyFlyApplication(LumenApplication, config_path=str(config_path))
    await application.startup()
    try:
        app = create_app(context=application.context, actuator_enabled=False, docs_enabled=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            before = await client.get("/api/v1/wallets/rollout/offer")
            with override_flags({"wallet-offer": True}):
                during = await client.get("/api/v1/wallets/rollout/offer")
            after = await client.get("/api/v1/wallets/rollout/offer")
        assert (before.status_code, before.json()) == (200, {"offer": "standard"})
        assert (during.status_code, during.json()) == (200, {"offer": "new"})
        assert (after.status_code, after.json()) == (200, {"offer": "standard"})
    finally:
        await application.shutdown()


@pytest.mark.asyncio
async def test_wallet_offer_store_conflict_and_outer_transaction(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'flags.db'}")
    store = SqlAlchemyFlagStore(engine)
    definition = {"state": "ENABLED", "variants": {"on": True, "off": False}, "defaultVariant": "on"}
    await store.start()
    try:
        async with infrastructure_unit(engine):
            await store.put("wallet-offer", definition, actor="lumen", expected_version=0)
        committed = await store.get("wallet-offer")
        assert committed is not None and committed.version == 1
        assert [(change.action, change.actor) for change in await store.history("wallet-offer")] == [("put", "lumen")]
        with pytest.raises(FlagConflictError):
            await store.put("wallet-offer", definition, actor="stale", expected_version=0)
        with pytest.raises(RuntimeError, match="cancel rollout"):
            async with infrastructure_unit(engine):
                await store.put("wallet-offer", {**definition, "defaultVariant": "off"}, actor="lumen")
                raise RuntimeError("cancel rollout")
        after = await store.get("wallet-offer")
        assert after is not None and after.version == 1 and after.definition["defaultVariant"] == "on"
        assert await store.revision() == 1
    finally:
        await store.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_preview_does_not_record_exposure_but_ordinary_evaluation_does() -> None:
    context = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "feature-flags": {
                        "enabled": "true",
                        "flags": {"wallet-offer": True},
                        "events": {"evaluations": "true"},
                    }
                }
            }
        )
    )
    context.register_bean(ExposureCollector)
    await context.start()
    try:
        collector = context.get_bean(ExposureCollector)
        management = context.get_bean(FlagManagement)
        flags = context.get_bean(FeatureFlags)
        preview = await management.evaluate("wallet-offer", targeting_key="alice")
        assert preview["value"] is True
        assert collector.events == []
        assert flags.is_enabled("wallet-offer", targeting_key="alice") is True
        await asyncio.wait_for(collector.ready.wait(), timeout=2)
        assert [(event.key, event.targeting_key) for event in collector.events] == [("wallet-offer", "alice")]
    finally:
        await context.stop()
