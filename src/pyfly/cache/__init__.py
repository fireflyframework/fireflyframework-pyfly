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
"""PyFly Cache — Cache abstraction with pluggable adapters.

Import concrete adapter types from the adapter package::

    from pyfly.cache.adapters.memory import InMemoryCache
    from pyfly.cache.adapters.redis import RedisCacheAdapter
"""

from pyfly.cache.decorators import cache, cache_evict, cache_put, cacheable
from pyfly.cache.manager import CacheManager
from pyfly.cache.ports.outbound import CacheAdapter

__all__ = [
    "CacheAdapter",
    "CacheManager",
    "cache",
    "cache_evict",
    "cache_put",
    "cacheable",
]
