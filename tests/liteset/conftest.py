# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Liteset test configuration.

Run liteset tests with: uv run pytest tests/liteset/
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase

pytest_plugins = ["pytest_asyncio"]


@asynccontextmanager
async def create_test_session(
    base: type[DeclarativeBase],
) -> AsyncIterator[AsyncSession]:
    """Factory for creating an async in-memory SQLite session.

    Creates all tables for the given DeclarativeBase, yields an
    AsyncSession, and disposes the engine on exit.

    Usage in test modules::

        @pytest.fixture
        async def async_session():
            async with create_test_session(Base) as session:
                yield session
    """
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
    await engine.dispose()
