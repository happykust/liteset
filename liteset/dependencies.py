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

from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, TypeVar

from litestar.connection import Request
from litestar.datastructures import State
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")


async def provide_async_session(state: State) -> AsyncGenerator[AsyncSession, None]:
    """Provide an AsyncSession with auto-commit/rollback."""
    async with state.session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


class RequestCache:
    """Per-request cache, replaces flask.g for memoization."""

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    async def get_or_set(
        self, key: str, factory: Callable[[], Awaitable[T]]
    ) -> T:
        if key not in self._store:
            self._store[key] = await factory()
        return self._store[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._store[key] = value


async def provide_request_cache(request: Request) -> RequestCache:
    """Per-request cache scoped to request lifecycle."""
    if not hasattr(request.state, "_cache"):
        request.state._cache = RequestCache()
    return request.state._cache


# --- flask.g user helper replacements ---


def get_current_user(request: Request) -> Any:
    return getattr(request, "user", None)


def get_user_id(request: Request) -> int | None:
    user = get_current_user(request)
    return getattr(user, "id", None) if user else None


def get_username(request: Request) -> str | None:
    user = get_current_user(request)
    return getattr(user, "username", None) if user else None
