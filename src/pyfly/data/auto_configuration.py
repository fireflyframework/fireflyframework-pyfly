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
"""Backend-neutral data auto-configuration: the auditing ports.

:class:`DataAuditingAutoConfiguration` provides the :class:`~pyfly.data.auditing.AuditorAware` and
:class:`~pyfly.data.auditing.DateTimeProvider` beans every backend's auditing uses (the relational
``AuditingEntityListener``, the document backend's hooks). Each backs off for a bean of the application's
own; ``pyfly.data.auditing.enabled=false`` switches auditing off altogether.
"""

# NOTE: No `from __future__ import annotations` — typing.get_type_hints()
# must resolve return types at runtime for @bean method registration.

from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration, conditional_on_missing_bean, conditional_on_property
from pyfly.data.auditing import AuditorAware, CurrentDateTimeProvider, DateTimeProvider, SecurityContextAuditorAware


@auto_configuration
@conditional_on_property("pyfly.data.auditing.enabled", having_value="true", match_if_missing=True)
class DataAuditingAutoConfiguration:
    """Who (``AuditorAware``) and when (``DateTimeProvider``) entities are stamped with.

    Declare a bean of either type to replace the default: declare it with the port as its return type
    (``-> AuditorAware``), or make its class subclass the port, so that it is injected by that type.
    """

    @bean
    @conditional_on_missing_bean(AuditorAware)
    def auditor_aware(self) -> AuditorAware:
        """The authenticated user of the security context (``SecurityContextHolder``), or ``None``."""
        return SecurityContextAuditorAware()

    @bean
    @conditional_on_missing_bean(DateTimeProvider)
    def date_time_provider(self) -> DateTimeProvider:
        """The UTC clock."""
        return CurrentDateTimeProvider()
