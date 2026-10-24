# Copyright 2026 Firefly Software Foundation. Licensed under the Apache License, Version 2.0.
"""Container construction and runtime ownership for Agentic entry points."""

from __future__ import annotations

import inspect
from typing import Any, get_type_hints

from pyfly.container.container import Container
from pyfly.container.exceptions import NoSuchBeanError, NoUniqueBeanError
from pyfly.container.registry import Registration
from pyfly.context.lifecycle import marked_method_names


async def construct_plugin(factory: Any, container: Container | None) -> tuple[Any, bool]:
    if container is None:
        result = factory()
    elif inspect.isclass(factory):
        registration = container.get_registration(factory)
        if registration is not None:
            return container.resolve(factory), True
        result = container._create_instance(Registration(factory))
    else:
        hints = get_type_hints(factory, include_extras=True)
        kwargs = {}
        for name, parameter in inspect.signature(factory).parameters.items():
            if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
                continue
            try:
                if name not in hints:
                    raise NoSuchBeanError(bean_type=None, parameter=name)
                kwargs[name] = container._resolve_param(hints[name])
            except (NoSuchBeanError, NoUniqueBeanError):
                if parameter.default is inspect.Parameter.empty:
                    raise
        result = factory(**kwargs)
    if inspect.isawaitable(result):
        result = await result
    if container is not None:
        if any(reg.instance is result for reg in container._all.values()):
            return result, True
        container._inject_autowired_fields(result)
    return result, False


class PluginResource:
    """Lifecycle owner for an entry point result that is not already a managed bean."""

    def __init__(self, instance: Any) -> None:
        self.instance = instance
        self.closed = False

    async def initialize(self) -> None:
        for name in marked_method_names(type(self.instance), "__pyfly_post_construct__"):
            result = getattr(self.instance, name)()
            if inspect.isawaitable(result):
                await result
        start = getattr(self.instance, "start", None)
        if (
            callable(start)
            and callable(getattr(self.instance, "stop", None))
            and "start" not in marked_method_names(type(self.instance), "__pyfly_post_construct__")
        ):
            result = start()
            if inspect.isawaitable(result):
                await result

    async def aclose(self) -> None:
        if self.closed:
            return
        names = marked_method_names(type(self.instance), "__pyfly_pre_destroy__")
        for name in names:
            result = getattr(self.instance, name)()
            if inspect.isawaitable(result):
                await result
        if not names:
            close = (
                getattr(self.instance, "aclose", None)
                or getattr(self.instance, "close", None)
                or getattr(self.instance, "stop", None)
                or getattr(self.instance, "dispose", None)
            )
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result
        self.closed = True


def native_memory_store(config: Any, framework_config: Any, container: Container | None) -> Any:
    if framework_config is None:
        raise ValueError(
            "Native database memory requires the host Config and native Data datasource; "
            "supply memory_store explicitly outside an ApplicationContext"
        )
    if config.memory_backend in ("relational", "postgres"):
        try:
            from pyfly.agentic.memory.relational_store import RelationalMemoryStore
            from pyfly.data.relational.framework_schema import (
                context_datasource_registry,
                creates_tables,
                module_datasource,
            )
        except ImportError as exc:
            raise RuntimeError("Relational Agentic memory requires pyfly[agentic-relational]") from exc
        registry = context_datasource_registry(framework_config, container)
        if config.memory_datasource:
            datasource = registry.get(config.memory_datasource)
        else:
            datasource = module_datasource(registry, framework_config, "pyfly.agentic.memory", name="agentic-memory")
        create = creates_tables(registry.properties.ddl_auto)
        if config.memory_create_table is True and not create:
            raise ValueError("memory_create_table=True requires native datasource ddl-auto=create or create-drop")
        create = create and config.memory_create_table is not False
        if config.memory_backend == "postgres":
            from pyfly.agentic.memory.database_store import PostgreSQLStore

            return PostgreSQLStore(
                datasource=datasource,
                schema_name=config.memory_postgres_schema,
                table=config.memory_table or "memory_entries",
                create_table=create,
            )
        return RelationalMemoryStore(
            datasource,
            table=config.memory_table or "pyfly_agentic_memory",
            schema=config.memory_schema,
            create_table=create,
        )
    try:
        from pymongo import AsyncMongoClient

        from pyfly.agentic.memory.database_store import MongoDBStore
        from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager
    except ImportError as exc:
        raise RuntimeError("MongoDB Agentic memory requires pyfly[agentic-mongodb]") from exc
    if container is None:
        raise ValueError(
            "MongoDB memory requires the native application client/transaction manager or an explicit memory_store"
        )
    manager = container.resolve(MongoTransactionManager)
    client = container.resolve(AsyncMongoClient)
    return MongoDBStore(
        client=client,
        manager=manager,
        database=config.memory_mongodb_database,
        collection=config.memory_mongodb_collection,
        create_indexes=config.memory_create_indexes,
    )
