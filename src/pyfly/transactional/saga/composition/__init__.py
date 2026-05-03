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
"""Saga composition — orchestrate multiple sagas as a DAG."""

from pyfly.transactional.saga.composition.compensation_manager import (
    CompensationManager,
)
from pyfly.transactional.saga.composition.composition import (
    CompositionEntry,
    SagaComposition,
    SagaDataFlow,
)
from pyfly.transactional.saga.composition.composition_builder import (
    SagaCompositionBuilder,
)
from pyfly.transactional.saga.composition.composition_context import (
    CompositionContext,
)
from pyfly.transactional.saga.composition.compositor import SagaCompositor
from pyfly.transactional.saga.composition.data_flow_manager import DataFlowManager
from pyfly.transactional.saga.composition.validator import CompositionValidator

__all__ = [
    "CompensationManager",
    "CompositionContext",
    "CompositionEntry",
    "CompositionValidator",
    "DataFlowManager",
    "SagaComposition",
    "SagaCompositionBuilder",
    "SagaCompositor",
    "SagaDataFlow",
]
