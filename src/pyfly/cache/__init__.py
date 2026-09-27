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

Caches hold copies, never live ORM objects (:class:`CacheValueError`); the decorators and
:class:`TransactionAwareCache` write after the unit of work commits; :func:`cache_region` and
:func:`dedicated_cache` give consumers named caches that clear independently.
"""

from pyfly.cache.decorators import cache, cache_evict, cache_put, cacheable
from pyfly.cache.manager import CacheManager
from pyfly.cache.namespaces import PrefixedCache, cache_region, dedicated_cache
from pyfly.cache.ports.outbound import CacheAdapter
from pyfly.cache.serialization import CacheValueError
from pyfly.cache.transaction import TransactionAwareCache

__all__ = [
    "CacheAdapter",
    "CacheManager",
    "CacheValueError",
    "PrefixedCache",
    "TransactionAwareCache",
    "cache",
    "cache_evict",
    "cache_put",
    "cache_region",
    "cacheable",
    "dedicated_cache",
]
