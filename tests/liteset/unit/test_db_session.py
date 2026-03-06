from __future__ import annotations

import pytest
from sqlalchemy import text

from liteset.db.session import (
    create_db_engine,
    create_session_factory,
    dispose_engine,
)


def test_create_engine() -> None:
    engine = create_db_engine("sqlite+aiosqlite://")
    assert engine is not None


def test_create_session_factory() -> None:
    engine = create_db_engine("sqlite+aiosqlite://")
    factory = create_session_factory(engine)
    assert factory is not None


async def test_session_factory_creates_working_session() -> None:
    engine = create_db_engine("sqlite+aiosqlite://")
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            result = await session.execute(text("SELECT 1"))
            assert result.scalar() == 1
    finally:
        await dispose_engine(engine)


async def test_dispose_engine() -> None:
    engine = create_db_engine("sqlite+aiosqlite://")
    await dispose_engine(engine)
