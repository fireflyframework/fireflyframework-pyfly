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
"""MongoDB data access adapter — Beanie ODM repositories on the unit of work.

Repositories (:class:`MongoRepository`), the transaction manager that runs ``@transactional`` on MongoDB
(:class:`MongoTransactionManager`), the base documents (:class:`BaseDocument` with maintained audit fields,
:class:`AggregateDocument` that raises domain events), the query layer (derived queries, ``@query``,
specifications and filter operators with SQL's semantics), and the health and metrics of the datasource.
"""

__all__: list[str] = []

try:
    from pyfly.data.document.mongodb.document import AggregateDocument, BaseDocument, DocumentAuditingHandler
    from pyfly.data.document.mongodb.filter import MongoFilterOperator, MongoFilterUtils
    from pyfly.data.document.mongodb.health import MongoHealthIndicator, MongoMetrics
    from pyfly.data.document.mongodb.initializer import BeanieInitializer, DocumentBindingError
    from pyfly.data.document.mongodb.post_processor import MongoRepositoryBeanPostProcessor
    from pyfly.data.document.mongodb.query import MongoAnnotatedQuery, MongoQueryExecutor
    from pyfly.data.document.mongodb.query_compiler import MongoDerivedQuery, MongoQueryMethodCompiler
    from pyfly.data.document.mongodb.repository import MongoRepository
    from pyfly.data.document.mongodb.specification import MongoSpecification
    from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager, current_session
    from pyfly.data.document.mongodb.transactional import mongo_transactional

    __all__ += [
        "AggregateDocument",
        "BaseDocument",
        "BeanieInitializer",
        "DocumentAuditingHandler",
        "DocumentBindingError",
        "MongoAnnotatedQuery",
        "MongoDerivedQuery",
        "MongoFilterOperator",
        "MongoFilterUtils",
        "MongoHealthIndicator",
        "MongoMetrics",
        "MongoQueryExecutor",
        "MongoQueryMethodCompiler",
        "MongoRepository",
        "MongoRepositoryBeanPostProcessor",
        "MongoSpecification",
        "MongoTransactionManager",
        "current_session",
        "mongo_transactional",
    ]
except ImportError:
    pass
