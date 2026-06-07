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
"""@ConditionalOnWebApplication / @ConditionalOnResource (v26.06.40)."""

from __future__ import annotations

from pathlib import Path

from pyfly.container.container import Container
from pyfly.context.condition_evaluator import ConditionEvaluator
from pyfly.context.conditions import conditional_on_resource, conditional_on_web_application
from pyfly.core.config import Config


def _evaluator() -> ConditionEvaluator:
    return ConditionEvaluator(Config({}), Container())


def test_conditional_on_web_application() -> None:
    @conditional_on_web_application()
    class WebBean:
        pass

    # starlette is installed in the test environment -> a web application
    assert _evaluator().should_include(WebBean) is True


def test_conditional_on_resource(tmp_path: Path) -> None:
    present_file = tmp_path / "present.txt"
    present_file.write_text("hi")

    @conditional_on_resource(str(present_file))
    class WithResource:
        pass

    @conditional_on_resource(str(tmp_path / "missing.txt"))
    class WithoutResource:
        pass

    evaluator = _evaluator()
    assert evaluator.should_include(WithResource) is True
    assert evaluator.should_include(WithoutResource) is False
