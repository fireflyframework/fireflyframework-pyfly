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
"""AOP auto-configuration — AspectBeanPostProcessor bean."""

# NOTE: No `from __future__ import annotations` — typing.get_type_hints()
# must resolve return types at runtime for @bean method registration.

from pyfly.aop.post_processor import AspectBeanPostProcessor
from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration


@auto_configuration
class AopAutoConfiguration:
    """Auto-configures the AspectBeanPostProcessor for AOP weaving."""

    @bean
    def aspect_post_processor(self) -> AspectBeanPostProcessor:
        return AspectBeanPostProcessor()
