"""A disposable local application with one native Data datasource."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from sqlalchemy import text

from pyfly.agentic.memory import MemoryManager
from pyfly.agentic.runtime import AgenticRuntime
from pyfly.context import ApplicationContext
from pyfly.core.config import Config
from pyfly.data.relational.datasource_registry import DataSource, DataSourceRegistry
from pyfly.data.transaction import TransactionTemplate


@asynccontextmanager
async def native_memory(path: Path) -> AsyncIterator[tuple[AgenticRuntime, DataSource]]:
    ctx = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "agentic": {
                        "enabled": True,
                        "observability-enabled": False,
                        "plugin-auto-discover": False,
                        "memory-backend": "relational",
                    },
                    "data": {
                        "relational": {
                            "url": f"sqlite+aiosqlite:///{path}",
                            "ddl-auto": "create",
                        }
                    },
                }
            }
        )
    )
    await ctx.start()
    try:
        datasource = ctx.get_bean(DataSourceRegistry).primary
        async with datasource.engine.begin() as connection:
            await connection.execute(text("CREATE TABLE IF NOT EXISTS review_requests (id TEXT PRIMARY KEY)"))
        yield ctx.get_bean(AgenticRuntime), datasource
    finally:
        await ctx.stop()


async def record_review(
    memory: MemoryManager,
    datasource: DataSource,
    request_id: str,
    *,
    abort: bool = False,
) -> None:
    async with TransactionTemplate(datasource).transaction() as unit:
        assert unit is not None
        await unit.resource.execute(
            text("INSERT INTO review_requests (id) VALUES (:id)"),
            {"id": request_id},
        )
        await memory.async_set_fact(f"review:{request_id}", {"status": "pending"})
        if abort:
            raise ValueError("cancel fictional review")


async def main() -> None:
    import json
    from tempfile import TemporaryDirectory

    with TemporaryDirectory(prefix="lumen-native-memory-") as directory:
        async with native_memory(Path(directory) / "lumen.db") as (runtime, datasource):
            await record_review(runtime.memory, datasource, "committed")
            with suppress(ValueError):
                await record_review(runtime.memory, datasource, "cancelled", abort=True)
            async with TransactionTemplate(datasource, read_only=True).transaction() as unit:
                assert unit is not None
                rows = (await unit.resource.execute(text("SELECT id FROM review_requests"))).scalars().all()
            print(
                json.dumps(
                    {
                        "business_rows": rows,
                        "committed_fact": await runtime.memory.async_get_fact("review:committed"),
                        "rolled_back_fact": await runtime.memory.async_get_fact("review:cancelled"),
                    }
                )
            )


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
