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
"""Aspect-Oriented Programming support for PyFly."""

from pyfly.aop.decorators import after, after_returning, after_throwing, around, aspect, before
from pyfly.aop.pointcut import matches_pointcut
from pyfly.aop.post_processor import AspectBeanPostProcessor
from pyfly.aop.registry import AdviceBinding, AspectRegistry
from pyfly.aop.types import JoinPoint
from pyfly.aop.weaver import weave_bean

__all__ = [
    "AdviceBinding",
    "AspectBeanPostProcessor",
    "AspectRegistry",
    "JoinPoint",
    "after",
    "after_returning",
    "after_throwing",
    "around",
    "aspect",
    "before",
    "matches_pointcut",
    "weave_bean",
]
