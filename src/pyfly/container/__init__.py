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
"""PyFly DI Container — Pythonic dependency injection."""

from pyfly.container.autowired import Autowired
from pyfly.container.bean import Qualifier, bean, primary
from pyfly.container.container import Container
from pyfly.container.exceptions import (
    BeanCreationException,
    BeanCurrentlyInCreationError,
    NoSuchBeanError,
    NoUniqueBeanError,
)
from pyfly.container.ordering import HIGHEST_PRECEDENCE, LOWEST_PRECEDENCE, order
from pyfly.container.stereotypes import (
    component,
    configuration,
    controller,
    controller_advice,
    repository,
    rest_controller,
    service,
)
from pyfly.container.types import Scope

__all__ = [
    "Autowired",
    "BeanCreationException",
    "BeanCurrentlyInCreationError",
    "Container",
    "HIGHEST_PRECEDENCE",
    "LOWEST_PRECEDENCE",
    "NoSuchBeanError",
    "NoUniqueBeanError",
    "Qualifier",
    "Scope",
    "bean",
    "component",
    "configuration",
    "controller",
    "controller_advice",
    "order",
    "primary",
    "repository",
    "rest_controller",
    "service",
]
