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
    """Provide an AsyncSession with auto-commit/rollback.

    Commits on success, rolls back on exception. Read-only requests
    incur a no-op commit (harmless). Session is managed manually
    (not via async with) to avoid double-rollback from the context
    manager's __aexit__.
    """
    session: AsyncSession = state.session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


class RequestCache:
    """Per-request cache, replaces flask.g for memoization.

    Not concurrency-safe — designed for single-task-per-request usage
    within Litestar's request lifecycle. Do not share across tasks.
    """

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    async def get_or_set(self, key: str, factory: Callable[[], Awaitable[T]]) -> T:
        if key not in self._store:
            self._store[key] = await factory()
        return self._store[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._store[key] = value


async def provide_request_cache() -> RequestCache:
    """Per-request cache scoped to request lifecycle.

    Registered with ``use_cache=True`` in the app factory, so Litestar
    guarantees at most one invocation per request — no manual caching needed.
    """
    return RequestCache()


# --- flask.g user helper replacements ---


def get_current_user(request: Request[Any, Any, Any]) -> Any:
    return getattr(request, "user", None)


def get_user_id(request: Request[Any, Any, Any]) -> int | None:
    user = get_current_user(request)
    return getattr(user, "id", None) if user else None


def get_username(request: Request[Any, Any, Any]) -> str | None:
    user = get_current_user(request)
    return getattr(user, "username", None) if user else None


async def provide_security_manager(
    session: AsyncSession,
    state: State,
) -> Any:
    """Provide AsyncSecurityManager scoped to the request session."""
    from superset.security.dao import AsyncSecurityDAO
    from superset.security.manager import AsyncSecurityManager

    dao = AsyncSecurityDAO(session)
    settings = state.settings
    # Resolve embedded_superset from dedicated setting or feature flags
    feature_flags = getattr(settings, "feature_flags", {})
    embedded_enabled = getattr(
        settings, "embedded_superset", False
    ) or feature_flags.get("EMBEDDED_SUPERSET", False)

    return AsyncSecurityManager(
        dao=dao,
        admin_role_name=settings.auth_role_admin,
        public_role_name=settings.auth_role_public,
        guest_role_name=settings.guest_role_name,
        dashboard_rbac_enabled=settings.dashboard_rbac,
        embedded_superset_enabled=embedded_enabled,
    )
