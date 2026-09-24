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
"""Auto-configure templates only when explicitly enabled."""

from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration, conditional_on_missing_bean, conditional_on_property
from pyfly.core.config import Config
from pyfly.web.templating.config import TemplateProperties
from pyfly.web.templating.ports import TemplateEngine


@auto_configuration
@conditional_on_property("pyfly.web.templates.enabled", having_value="true")
class TemplateAutoConfiguration:
    @bean
    @conditional_on_missing_bean(TemplateEngine)
    def template_engine(self, config: Config) -> TemplateEngine:
        try:
            from pyfly.web.templating.adapters.jinja import JinjaTemplateEngine
        except ImportError as exc:
            raise RuntimeError("Templates require pyfly[templates] or a TemplateEngine bean") from exc
        return JinjaTemplateEngine(config.bind(TemplateProperties))
