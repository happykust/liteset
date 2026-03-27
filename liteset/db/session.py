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
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    AsyncEngine,
    AsyncSession,
    create_async_engine as _create_async_engine,
)


def create_db_engine(
    url: str,
    echo: bool = False,
    pool_size: int = 5,
    max_overflow: int = 10,
    pool_timeout: int = 30,
    pool_recycle: int = 3600,
    pool_pre_ping: bool = True,
) -> AsyncEngine:
    global _engine  # noqa: PLW0603
    kwargs: dict[str, Any] = {"echo": echo}
    if not url.startswith("sqlite"):
        kwargs["pool_size"] = pool_size
        kwargs["max_overflow"] = max_overflow
        kwargs["pool_timeout"] = pool_timeout
        kwargs["pool_recycle"] = pool_recycle
        kwargs["pool_pre_ping"] = pool_pre_ping
    engine = _create_async_engine(url, **kwargs)
    _engine = engine
    return engine


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


_engine: AsyncEngine | None = None


def get_engine() -> AsyncEngine:
    """Return the module-level engine reference.

    Raises RuntimeError if no engine has been created yet via
    :func:`create_db_engine`.
    """
    if _engine is None:
        raise RuntimeError("No engine has been created yet")
    return _engine


async def dispose_engine(engine: AsyncEngine) -> None:
    await engine.dispose()
