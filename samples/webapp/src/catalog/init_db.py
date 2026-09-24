"""Explicit development-only creation of the sample's table; never drops data."""

import asyncio
import os

from sqlalchemy.ext.asyncio import create_async_engine

from catalog.models import Product


async def initialize() -> None:
    engine = create_async_engine(os.environ.get("WEBAPP_DATABASE_URL", "sqlite+aiosqlite:///catalog.db"))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Product.__table__.create, checkfirst=True)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(initialize())
