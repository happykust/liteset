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
# mypy: ignore-errors
"""Celery task that triggers a database permissions sync.

``SyncPermissionsCommand`` is async, so we run it inside an event loop
spawned in the worker thread (matching the pattern used by
:mod:`superset.tasks.async_queries`).
"""

from __future__ import annotations

import asyncio
import logging

from superset.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="sync_database_permissions", soft_time_limit=600)
def sync_database_permissions_task(
    database_id: int, username: str, old_db_connection_name: str
) -> None:
    """Trigger SyncPermissionsCommand.sync_database_permissions."""
    try:
        asyncio.run(
            _run(
                database_id=database_id,
                username=username,
                old_db_connection_name=old_db_connection_name,
            )
        )
        logger.info("Successfully synced permissions for DB connection %s", database_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            "An error occurred while syncing permissions for DB connection ID %s",
            database_id,
        )


async def _run(
    database_id: int,
    username: str,
    old_db_connection_name: str,
) -> None:
    from superset.commands.database.sync_permissions import SyncPermissionsCommand
    from superset.config import SupersetSettings
    from superset.db.daos.database import AsyncDatabaseDAO
    from superset.db.session import create_session_factory, get_engine
    from superset.security.dao import AsyncSecurityDAO
    from superset.security.manager import build_async_security_manager
    from superset.utils.core import set_current_user

    engine = get_engine()
    factory = create_session_factory(engine)

    async with factory() as session:
        security_dao = AsyncSecurityDAO(session)
        user = await security_dao.get_user_by_username(username)
        if user is None:
            logger.error(
                "Cannot run sync_database_permissions: user %s not found", username
            )
            return

        set_current_user(user)
        logger.info(
            "Syncing permissions for DB connection %s while impersonating user %s",
            database_id,
            getattr(user, "id", "?"),
        )

        dao = AsyncDatabaseDAO(session)
        database = await dao.find_by_id(database_id)
        if database is None:
            logger.error(
                "Cannot run sync_database_permissions: database %s not found",
                database_id,
            )
            return

        ssh_tunnel = await dao.get_ssh_tunnel(database_id)

        security_manager = build_async_security_manager(session, SupersetSettings())
        cmd = SyncPermissionsCommand(
            dao=dao,
            database_id=database_id,
            security_manager=security_manager,
            username=username,
            old_db_connection_name=old_db_connection_name,
            db_connection=database,
            ssh_tunnel=ssh_tunnel,
        )
        # Call sync_database_permissions directly — not run() (which would re-dispatch)
        # and not validate() (no pre-flight ping), matching the original task.
        await cmd.sync_database_permissions()
        # No request middleware to commit the AsyncSession — commit explicitly
        # or the new/renamed permission rows are lost.
        await session.commit()
