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
"""Port of ``tests/unit_tests/databases/ssh_tunnel/commands/create_test.py``.

Verifies ``CreateSSHTunnelCommand`` validation and creation behaviour:
a valid payload yields an ``SSHTunnel``; a private-key-password without a
private key raises ``SSHTunnelInvalidError``; a database URI without an
explicit port falls back to the backend default port, and a URI for a
backend with no default port raises ``SSHTunnelDatabasePortError``.

Adapted for the Liteset async port: the command takes an
``AsyncSSHTunnelDAO`` and is awaited, and DAO operations run against an
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
async def dao_env():
    import superset.models  # noqa: F401  (register models)
    from superset.db.daos.database import AsyncSSHTunnelDAO
    from superset.models.helpers import Base

    sync_engine = create_engine("sqlite://")
    Base.metadata.create_all(sync_engine)
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        creator=lambda: sync_engine.raw_connection(),
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield AsyncSSHTunnelDAO(session), session
    await engine.dispose()


async def _make_database(session, sqlalchemy_uri: str):
    from superset.models.core import Database

    database = Database(
        database_name="my_database",
        sqlalchemy_uri=sqlalchemy_uri,
    )
    session.add(database)
    await session.flush()
    return database


async def test_create_ssh_tunnel_command(dao_env) -> None:
    from superset.commands.database.ssh_tunnel.create import CreateSSHTunnelCommand
    from superset.models.ssh_tunnel import SSHTunnel

    dao, session = dao_env
    database = await _make_database(session, "postgresql://u:p@localhost:5432/db")

    properties = {
        "server_address": "123.132.123.1",
        "server_port": "3005",
        "username": "foo",
        "password": "bar",
    }

    result = await CreateSSHTunnelCommand(dao, database, properties).execute()

    assert result is not None
    assert isinstance(result, SSHTunnel)


async def test_create_ssh_tunnel_command_invalid_params(dao_env) -> None:
    from superset.commands.database.ssh_tunnel.create import CreateSSHTunnelCommand

    dao, session = dao_env
    database = await _make_database(session, "postgresql://u:p@localhost:5432/db")

    # If we are trying to create a tunnel with a private_key_password
    # then a private_key is mandatory
    properties = {
        "server_address": "123.132.123.1",
        "server_port": "3005",
        "username": "foo",
        "private_key_password": "bar",
    }

    command = CreateSSHTunnelCommand(dao, database, properties)

    with pytest.raises(SSHTunnelInvalidError) as excinfo:
        await command.execute()
    assert str(excinfo.value) == ("SSH Tunnel parameters are invalid.")


async def test_create_ssh_tunnel_command_no_port(dao_env) -> None:
    """
    Test that SSH Tunnel can be created without explicit port but with a default one.
    """
    from superset.commands.database.ssh_tunnel.create import CreateSSHTunnelCommand
    from superset.models.ssh_tunnel import SSHTunnel

    dao, session = dao_env
    database = await _make_database(session, "postgresql://u:p@localhost/db")

    properties = {
        "server_address": "123.132.123.1",
        "server_port": "3005",
        "username": "foo",
        "password": "bar",
    }

    result = await CreateSSHTunnelCommand(dao, database, properties).execute()

    assert result is not None
    assert isinstance(result, SSHTunnel)


async def test_create_ssh_tunnel_command_no_port_no_default(dao_env) -> None:
    """
    Test that error is raised when creating SSH Tunnel without explicit/default ports.
    """
    from superset.commands.database.ssh_tunnel.create import CreateSSHTunnelCommand

    dao, session = dao_env
    database = await _make_database(session, "weird+db://u:p@localhost/db")

    properties = {
        "server_address": "123.132.123.1",
        "server_port": "3005",
        "username": "foo",
        "password": "bar",
    }

    command = CreateSSHTunnelCommand(dao, database, properties)

    with pytest.raises(SSHTunnelDatabasePortError) as excinfo:
        await command.execute()
    assert str(excinfo.value) == (
        "A database port is required when connecting via SSH Tunnel."
    )
