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
"""``superset load-test-users`` CLI parity.

The custom test roles ``gamma_sqllab`` / ``gamma_no_csv`` must be granted
``database_access`` on the example database — 1:1 with upstream
``cli/test.py`` (which adds ``examples_pv`` to both roles). Standard Gamma /
sql_lab role defs do not carry that grant, so copying their permissions is
not sufficient.
"""

from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session


def test_load_test_users_grants_examples_db_access(monkeypatch) -> None:
    import superset.models  # noqa: F401  (register models)
    from superset.cli import users as users_cli
    from superset.models.core import Database
    from superset.models.helpers import Base
    from superset.models.security import Role

    monkeypatch.setenv("LITESET_TESTING", "true")

    # One shared in-memory DB (SingletonThreadPool → same connection), so the
    # async session the CLI opens and our sync verification session see the
    # same committed rows.
    sync_engine = create_engine("sqlite://")
    Base.metadata.create_all(sync_engine)
    async_engine = create_async_engine(
        "sqlite+aiosqlite://",
        creator=lambda: sync_engine.raw_connection(),
    )
    factory = async_sessionmaker(async_engine, expire_on_commit=False)

    monkeypatch.setattr(
        users_cli, "_get_async_session_factory", lambda: (factory, async_engine)
    )

    # Invoke the click command's underlying callback.
    users_cli.load_test_users.callback("general")

    with Session(sync_engine) as s:
        examples = (
            s.execute(select(Database).where(Database.database_name == "examples"))
            .scalars()
            .one()
        )
        examples_perm = examples.perm
        for role_name in ("gamma_sqllab", "gamma_no_csv"):
            role = (
                s.execute(select(Role).where(Role.name == role_name)).scalars().one()
            )
            granted = {
                (pv.permission.name, pv.view_menu.name) for pv in role.permissions
            }
            assert ("database_access", examples_perm) in granted, (
                f"{role_name} missing example-DB database_access; has {granted}"
            )
