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
"""Admin dashboard auto-configuration."""

from __future__ import annotations

from pyfly.admin.config import AdminClientProperties, AdminProperties
from pyfly.admin.log_handler import AdminLogHandler
from pyfly.admin.middleware.trace_collector import TraceCollectorFilter
from pyfly.admin.providers.runtime_provider import RuntimeProvider
from pyfly.admin.registry import AdminViewRegistry
from pyfly.admin.server.client_registration import AdminClientRegistration
from pyfly.container.bean import bean
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_class,
    conditional_on_property,
)
from pyfly.core.config import Config


@auto_configuration
@conditional_on_property("pyfly.admin.enabled", having_value="true")
@conditional_on_class("starlette")
class AdminAutoConfiguration:
    """Auto-configures the admin dashboard when enabled."""

    @bean
    def admin_properties(self, config: Config) -> AdminProperties:
        return config.bind(AdminProperties)

    @bean
    def admin_view_registry(self) -> AdminViewRegistry:
        return AdminViewRegistry()

    @bean
    def runtime_provider(self) -> RuntimeProvider:
        return RuntimeProvider()

    @bean
    def admin_trace_collector(self) -> TraceCollectorFilter:
        return TraceCollectorFilter()

    @bean
    def admin_log_handler(self) -> AdminLogHandler:
        import logging

        handler = AdminLogHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger().addHandler(handler)
        return handler

    @bean
    @conditional_on_property("pyfly.admin.client.url")
    def admin_client_registration(self, config: Config) -> AdminClientRegistration | None:
        """Create a client registration bean when an admin server URL is set."""
        client_props: AdminClientProperties = config.bind(AdminClientProperties)
        if not client_props.url:
            return None
        app_name = config.get("pyfly.app.name", "unknown")
        app_url = config.get("pyfly.app.url", "http://localhost:8080")
        return AdminClientRegistration(
            admin_server_url=client_props.url,
            app_name=app_name,
            app_url=app_url,
        )
