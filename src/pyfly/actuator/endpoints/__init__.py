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
"""Built-in actuator endpoint implementations."""

from pyfly.actuator.endpoints.beans_endpoint import BeansEndpoint
from pyfly.actuator.endpoints.caches_endpoint import CachesEndpoint
from pyfly.actuator.endpoints.conditions_endpoint import ConditionsEndpoint
from pyfly.actuator.endpoints.configprops_endpoint import ConfigPropsEndpoint
from pyfly.actuator.endpoints.env_endpoint import EnvEndpoint
from pyfly.actuator.endpoints.health_endpoint import HealthEndpoint
from pyfly.actuator.endpoints.httpexchanges_endpoint import HttpExchangesEndpoint
from pyfly.actuator.endpoints.info_endpoint import InfoEndpoint
from pyfly.actuator.endpoints.loggers_endpoint import LoggersEndpoint
from pyfly.actuator.endpoints.mappings_endpoint import MappingsEndpoint
from pyfly.actuator.endpoints.metrics_endpoint import MetricsEndpoint
from pyfly.actuator.endpoints.prometheus_endpoint import PrometheusEndpoint
from pyfly.actuator.endpoints.scheduledtasks_endpoint import ScheduledTasksEndpoint
from pyfly.actuator.endpoints.threaddump_endpoint import ThreadDumpEndpoint

__all__ = [
    "BeansEndpoint",
    "CachesEndpoint",
    "ConditionsEndpoint",
    "ConfigPropsEndpoint",
    "EnvEndpoint",
    "HealthEndpoint",
    "HttpExchangesEndpoint",
    "InfoEndpoint",
    "LoggersEndpoint",
    "MappingsEndpoint",
    "MetricsEndpoint",
    "PrometheusEndpoint",
    "ScheduledTasksEndpoint",
    "ThreadDumpEndpoint",
]
