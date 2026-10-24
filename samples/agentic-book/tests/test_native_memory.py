"""Business SQL and working facts share the native SQLite transaction."""

import pytest
from lumen_agentic.native_memory import native_memory, record_review
from sqlalchemy import text

from pyfly.data.transaction import TransactionTemplate


@pytest.mark.asyncio
async def test_review_and_async_fact_commit_together(tmp_path):
    async with native_memory(tmp_path / "lumen.db") as (runtime, datasource):
        await record_review(runtime.memory, datasource, "review-1")
        assert await runtime.memory.async_get_fact("review:review-1") == {"status": "pending"}
        async with TransactionTemplate(datasource, read_only=True).transaction() as unit:
            rows = (await unit.resource.execute(text("SELECT id FROM review_requests"))).scalars().all()
        assert rows == ["review-1"]
        await runtime.memory.async_set_fact("revision", {"number": 1})
        await runtime.memory.async_set_fact("revision", {"number": 2})
        assert await runtime.memory.async_get_fact("revision") == {"number": 2}


@pytest.mark.asyncio
async def test_review_and_async_fact_rollback_together(tmp_path):
    async with native_memory(tmp_path / "lumen.db") as (runtime, datasource):
        with pytest.raises(ValueError, match="cancel fictional review"):
            await record_review(runtime.memory, datasource, "review-2", abort=True)
        assert await runtime.memory.async_get_fact("review:review-2") is None
        async with TransactionTemplate(datasource, read_only=True).transaction() as unit:
            assert (await unit.resource.execute(text("SELECT count(*) FROM review_requests"))).scalar_one() == 0
