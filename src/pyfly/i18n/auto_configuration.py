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
"""I18n subsystem auto-configuration."""

from __future__ import annotations

from pyfly.container.bean import bean
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_missing_bean,
    conditional_on_property,
)
from pyfly.core.config import Config
from pyfly.i18n.adapters.resource_bundle import ResourceBundleMessageSource
from pyfly.i18n.locale import AcceptHeaderLocaleResolver, LocaleResolver
from pyfly.i18n.ports.outbound import MessageSource


@auto_configuration
@conditional_on_property("pyfly.i18n.enabled", having_value="true")
class I18nAutoConfiguration:
    """Auto-configures message source and locale resolver beans."""

    @bean
    @conditional_on_missing_bean(MessageSource)
    def message_source(self, config: Config) -> ResourceBundleMessageSource:
        base_path = str(config.get("pyfly.i18n.base-path", "i18n/"))
        default_locale = str(config.get("pyfly.i18n.default-locale", "en"))
        return ResourceBundleMessageSource(
            base_path=base_path,
            default_locale=default_locale,
        )

    @bean
    @conditional_on_missing_bean(LocaleResolver)
    def locale_resolver(self, config: Config) -> AcceptHeaderLocaleResolver:
        default_locale = str(config.get("pyfly.i18n.default-locale", "en"))
        return AcceptHeaderLocaleResolver(default_locale=default_locale)
