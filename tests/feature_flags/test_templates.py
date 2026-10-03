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
"""Feature flag functions in rendered web templates."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from pyfly.container import controller
from pyfly.container.exceptions import NoSuchBeanError
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.feature_flags.templates import FeatureFlagsTemplateContext
from pyfly.web import ModelAndView, get_mapping
from pyfly.web.templating.ports import TemplateContextProcessor

PAGE = (
    "{% if feature_flag('new-ui') %}NEW{% else %}OLD{% endif %}|{{ feature_variant('theme') }}|"
    "{{ feature_flag('missing') }}|{{ feature_flag('missing', True) }}|{{ feature_variant('missing') }}"
)


@controller
class Pages:
    @get_mapping("/page")
    async def page(self) -> ModelAndView:
        return ModelAndView("page.html")


def _config(templates: Path, *, enabled: bool = True) -> Config:
    return Config(
        {
            "pyfly": {
                "web": {"templates": {"enabled": enabled, "directories": [str(templates)]}},
                "feature-flags": {"enabled": "true", "flags": {"new-ui": True, "theme": "dark"}},
            }
        }
    )


async def test_rendered_views_see_flag_functions_and_caller_defaults(tmp_path: Path) -> None:
    from pyfly.web.adapters.starlette.app import create_app

    (tmp_path / "page.html").write_text(PAGE, encoding="utf-8")
    context = ApplicationContext(_config(tmp_path))
    context.register_bean(Pages)
    await context.start()
    try:
        processor = context.get_bean(FeatureFlagsTemplateContext)
        assert isinstance(processor, TemplateContextProcessor)
        app = create_app(context=context, actuator_enabled=False, docs_enabled=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/page")
        assert response.status_code == 200
        assert response.text == "NEW|dark|False|True|None"
    finally:
        await context.stop()


async def test_disabled_templates_do_not_register_a_flag_processor(tmp_path: Path) -> None:
    context = ApplicationContext(_config(tmp_path, enabled=False))
    await context.start()
    try:
        with pytest.raises(NoSuchBeanError):
            context.get_bean(FeatureFlagsTemplateContext)
    finally:
        await context.stop()


async def test_context_exposes_both_callable_functions() -> None:
    class Flags:
        def is_enabled(self, key: str, default: bool = False) -> bool:
            return key == "on" or default

        def variant(self, key: str) -> str | None:
            return "b" if key == "ab" else None

    processor = FeatureFlagsTemplateContext(Flags())  # type: ignore[arg-type]
    values: dict[str, Any] = dict(await processor.get_context(request=None))
    assert set(values) == {"feature_flag", "feature_variant"}
    assert values["feature_flag"]("on") is True and values["feature_flag"]("x") is False
    assert values["feature_flag"]("x", True) is True and values["feature_variant"]("ab") == "b"
