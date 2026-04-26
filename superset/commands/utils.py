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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from superset.exceptions import (
    OwnersNotFoundValidationError,
    RolesNotFoundValidationError,
)


async def populate_roles(
    session: AsyncSession,
    role_ids: list[int] | None,
) -> list[Any]:
    """Resolve role ids to ``Role`` objects.

    Async port of ``superset_old/commands/utils.py:populate_roles``.
    Returns ``[]`` for an empty/None list. Raises
    :class:`RolesNotFoundValidationError` if any id can't be resolved.
    """
    from superset.models.security import Role

    role_ids = role_ids or []
    if not role_ids:
        return []
    stmt = select(Role).where(Role.id.in_(role_ids))
    result = await session.execute(stmt)
    roles = list(result.scalars().all())
    if len(roles) != len(role_ids):
        raise RolesNotFoundValidationError()
    return roles


async def populate_owner_list(
    security_manager: Any,
    current_user_id: int | None,
    owner_ids: list[int] | None,
    *,
    default_to_user: bool,
) -> list[Any]:
    """Resolve owner ids to User objects.

    Async port of ``superset_old/commands/utils.py:populate_owner_list``.

    - When ``owner_ids`` is empty and ``default_to_user`` is True, the
      current user becomes the only owner.
    - Non-admin callers cannot remove themselves: if the current user
      isn't admin and isn't in ``owner_ids``, they're prepended.
    - Raises :class:`OwnersNotFoundValidationError` if any id can't
      be resolved.
    """
    owner_ids = owner_ids or []
    owners: list[Any] = []
    current_user = (
        await security_manager.find_user_by_id(current_user_id)
        if current_user_id is not None
        else None
    )
    if not owner_ids and default_to_user:
        return [current_user] if current_user else []
    is_admin = security_manager.is_admin(current_user) if current_user else True
    if current_user is not None and not is_admin and current_user.id not in owner_ids:
        owners.append(current_user)
    for owner_id in owner_ids:
        owner = await security_manager.find_user_by_id(owner_id)
        if not owner:
            raise OwnersNotFoundValidationError()
        owners.append(owner)
    return owners


async def compute_owner_list(
    security_manager: Any,
    current_user_id: int | None,
    current_owners: list[Any] | None,
    new_owner_ids: list[int] | None,
) -> list[Any]:
    """Compute final owner list for an update.

    Async port of ``superset_old/commands/utils.py:compute_owner_list``.
    Preserves existing owners when ``new_owner_ids`` is ``None``;
    otherwise resolves and validates the supplied ids.
    """
    current_owners = current_owners or []
    owner_ids = (
        [owner.id for owner in current_owners]
        if new_owner_ids is None
        else new_owner_ids
    )
    return await populate_owner_list(
        security_manager,
        current_user_id,
        owner_ids,
        default_to_user=False,
    )
