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
"""Port of ``tests/unit_tests/databases/ssh_tunnel/commands/update_test.py``.

Verifies ``UpdateSSHTunnelCommand`` behaviour: a valid ``server_address``
update persists; a ``private_key_password`` without a ``private_key`` raises
``SSHTunnelInvalidError``; a database URI without an explicit port falls back
to the backend default; and a URI for a backend with no default port raises
``SSHTunnelDatabasePortError``.

Adapted for the Liteset async port: the command takes an
``AsyncSSHTunnelDAO`` and is awaited; the tunnel is looked up via
``AsyncSSHTunnelDAO.get_by_database_id`` (the upstream
``DatabaseDAO.get_ssh_tunnel`` equivalent); DAO operations run against an
``AsyncSession`` backed by an in-memory SQLite engine.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from superset.commands.database.ssh_tunnel.exceptions import (
    SSHTunnelDatabasePortError,
    SSHTunnelInvalidError,
)


@pytest.fixture
async def dao_env(request):
    import superset.models  # noqa: F401  (register models)
    from superset.db.daos.database import AsyncSSHTunnelDAO
    from superset.models.core import Database
    from superset.models.helpers import Base
    from superset.models.ssh_tunnel import SSHTunnel

    sync_engine = create_engine("sqlite://")
    Base.metadata.create_all(sync_engine)
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        creator=lambda: sync_engine.raw_connection(),
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    sqlalchemy_uri = getattr(request, "param", "postgresql://u:p@localhost:5432/db")
    async with session_factory() as session:
        database = Database(database_name="my_database", sqlalchemy_uri=sqlalchemy_uri)
        session.add(database)
        await session.flush()
        ssh_tunnel = SSHTunnel(
            database_id=database.id,
            database=database,
            server_address="Test",
        )
        session.add(ssh_tunnel)
        await session.flush()
        yield AsyncSSHTunnelDAO(session), database.id


async def test_update_shh_tunnel_command(dao_env) -> None:
    from superset.commands.database.ssh_tunnel.update import UpdateSSHTunnelCommand
    from superset.models.ssh_tunnel import SSHTunnel

    dao, database_id = dao_env

    result = await dao.get_by_database_id(database_id)

    assert result
    assert isinstance(result, SSHTunnel)
    assert database_id == result.database_id
    assert "Test" == result.server_address

    update_payload = {"server_address": "Test2"}
    await UpdateSSHTunnelCommand(dao, result.id, update_payload).execute()

    result = await dao.get_by_database_id(database_id)

    assert result
    assert isinstance(result, SSHTunnel)
    assert "Test2" == result.server_address


async def test_update_shh_tunnel_invalid_params(dao_env) -> None:
    from superset.commands.database.ssh_tunnel.update import UpdateSSHTunnelCommand
    from superset.models.ssh_tunnel import SSHTunnel

    dao, database_id = dao_env

    result = await dao.get_by_database_id(database_id)

    assert result
    assert isinstance(result, SSHTunnel)
    assert database_id == result.database_id
    assert "Test" == result.server_address

    # If we are trying to update a tunnel with a private_key_password
    # then a private_key is mandatory
    update_payload = {"private_key_password": "pass"}
    command = UpdateSSHTunnelCommand(dao, result.id, update_payload)

    with pytest.raises(SSHTunnelInvalidError) as excinfo:
        await command.execute()
    assert str(excinfo.value) == ("SSH Tunnel parameters are invalid.")


@pytest.mark.parametrize(
    "dao_env", ["postgresql://u:p@localhost/testdb"], indirect=True
)
async def test_update_shh_tunnel_no_port(dao_env) -> None:
    """
    Test that SSH Tunnel can be updated without explicit port but with a default one.
    """
    from superset.commands.database.ssh_tunnel.update import UpdateSSHTunnelCommand
    from superset.models.ssh_tunnel import SSHTunnel

    dao, database_id = dao_env

    result = await dao.get_by_database_id(database_id)

    assert result
    assert isinstance(result, SSHTunnel)
    assert database_id == result.database_id
    assert "Test" == result.server_address

    update_payload = {"server_address": "Test2"}
    await UpdateSSHTunnelCommand(dao, result.id, update_payload).execute()

    result = await dao.get_by_database_id(database_id)

    assert result
    assert isinstance(result, SSHTunnel)
    assert "Test2" == result.server_address


@pytest.mark.parametrize("dao_env", ["weird+db://u:p@localhost/testdb"], indirect=True)
async def test_update_shh_tunnel_no_port_no_default(dao_env) -> None:
    """
    Test that error is raised when updating SSH Tunnel without explicit/default ports.
    """
    from superset.commands.database.ssh_tunnel.update import UpdateSSHTunnelCommand
    from superset.models.ssh_tunnel import SSHTunnel

    dao, database_id = dao_env

    result = await dao.get_by_database_id(database_id)

    assert result
    assert isinstance(result, SSHTunnel)
    assert database_id == result.database_id
    assert "Test" == result.server_address

    update_payload = {"server_address": "Test update"}
    command = UpdateSSHTunnelCommand(dao, result.id, update_payload)

    with pytest.raises(SSHTunnelDatabasePortError) as excinfo:
        await command.execute()
    assert str(excinfo.value) == (
        "A database port is required when connecting via SSH Tunnel."
    )
