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
"""PyFly Context — ApplicationContext, lifecycle, events, and conditions."""

from pyfly.context.application_context import ApplicationContext
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_bean,
    conditional_on_class,
    conditional_on_expression,
    conditional_on_missing_bean,
    conditional_on_property,
    conditional_on_resource,
    conditional_on_single_candidate,
    conditional_on_web_application,
)
from pyfly.context.environment import Environment
from pyfly.context.events import (
    ApplicationEvent,
    ApplicationEventBus,
    ApplicationEventPublisher,
    ApplicationReadyEvent,
    ContextClosedEvent,
    ContextRefreshedEvent,
    RefreshScopeRefreshedEvent,
    app_event_listener,
)
from pyfly.context.lifecycle import post_construct, pre_destroy
from pyfly.context.post_processor import BeanPostProcessor
from pyfly.context.refresh import ContextRefresher
from pyfly.context.request_context import RequestContext

__all__ = [
    "ApplicationContext",
    "ApplicationEvent",
    "ApplicationEventBus",
    "ApplicationEventPublisher",
    "ApplicationReadyEvent",
    "BeanPostProcessor",
    "ContextClosedEvent",
    "ContextRefreshedEvent",
    "ContextRefresher",
    "RefreshScopeRefreshedEvent",
    "Environment",
    "RequestContext",
    "app_event_listener",
    "auto_configuration",
    "conditional_on_bean",
    "conditional_on_class",
    "conditional_on_missing_bean",
    "conditional_on_expression",
    "conditional_on_property",
    "conditional_on_resource",
    "conditional_on_single_candidate",
    "conditional_on_web_application",
    "post_construct",
    "pre_destroy",
]
