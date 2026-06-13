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
"""Round-10 importer regressions.

* The dataset importer's ``_import_database`` used to ``setattr`` the YAML
  ``extra`` dict straight onto the ``Text`` column (DBAPI bind error), skipped
  the ``PREVENT_UNSAFE_DB_CONNECTIONS`` URI check, never masked the password
  via ``set_sqlalchemy_uri`` and never called ``add_permissions``.  It now
  delegates to the shared full ``_import_database`` port.
* ``_import_chart``'s overwrite-permission block called
  ``can_access_chart(existing)`` without the keyword-only ``user`` and
  ``await``-ed the synchronous ``is_admin()`` without its ``user`` argument —
  TypeError on any authenticated overwrite import.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from superset.utils import json


@pytest.fixture
async def import_env():
    import superset.models  # noqa: F401  (register models)
    from superset.models.core import Database
    from superset.models.helpers import Base
    from superset.models.security import User

    sync_engine = create_engine("sqlite://")
    Base.metadata.create_all(sync_engine)
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        creator=lambda: sync_engine.raw_connection(),
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        db = Database(
            database_name="examples",
            sqlalchemy_uri="sqlite://",
            uuid="aaaaaaaa-0000-0000-0000-000000000001",
        )
        user = User(
            username="importer",
            first_name="im",
            last_name="porter",
            email="importer@test.com",
            active=True,
        )
        session.add_all([db, user])
        await session.commit()
        yield session, db, user
    await engine.dispose()


def _make_dataset_cmd(session):
    from superset.commands.dataset.importers.v1 import ImportDatasetsCommand
    from superset.db.daos.dataset import AsyncDatasetDAO

    cmd = ImportDatasetsCommand.__new__(ImportDatasetsCommand)
    cmd._dao = AsyncDatasetDAO(session)
    cmd._security_manager = None
    cmd._ignore_permissions = True
    cmd._sync_columns = False
    cmd._sync_metrics = False
    cmd._force_data = False
    cmd._overwrite = False
    return cmd


@pytest.fixture
def add_permissions_calls(monkeypatch):
    """Record add_permissions calls instead of hitting the network."""
    import superset.commands.chart.importers.v1.utils as chart_utils

    calls: list = []

    async def _record(session, database, ssh_tunnel=None):
        calls.append(database)

    monkeypatch.setattr(chart_utils, "add_permissions", _record)
    return calls


async def test_dataset_import_database_extra_json_dumps(
    import_env, add_permissions_calls
):
    """crit: YAML ``extra`` dict must be JSON-serialised before the bind."""
    from superset.models.core import Database

    session, _db, _user = import_env
    cmd = _make_dataset_cmd(session)

    await cmd._import_database(
        "databases/imported.yaml",
        {
            "uuid": "dddddddd-0000-0000-0000-000000000001",
            "database_name": "imported_db",
            "sqlalchemy_uri": "postgresql://user:secret@localhost:5432/db",
            "extra": {"allows_virtual_table_explore": True},
            "allow_csv_upload": False,
        },
    )
    await session.commit()

    imported = (
        (
            await session.execute(
                select(Database).where(
                    Database.uuid == "dddddddd-0000-0000-0000-000000000001"
                )
            )
        )
        .scalars()
        .one()
    )
    assert isinstance(imported.extra, str)
    assert json.loads(imported.extra) == {"allows_virtual_table_explore": True}


async def test_dataset_import_database_masks_password(
    import_env, add_permissions_calls
):
    """med: URI stored masked via set_sqlalchemy_uri, real password on column."""
    from superset.models.core import Database

    session, _db, _user = import_env
    cmd = _make_dataset_cmd(session)

    await cmd._import_database(
        "databases/imported.yaml",
        {
            "uuid": "dddddddd-0000-0000-0000-000000000002",
            "database_name": "masked_db",
            "sqlalchemy_uri": "postgresql://user:supersecret@localhost:5432/db",
            "extra": {},
        },
    )
    await session.commit()

    imported = (
        (
            await session.execute(
                select(Database).where(
                    Database.uuid == "dddddddd-0000-0000-0000-000000000002"
                )
            )
        )
        .scalars()
        .one()
    )
    assert "supersecret" not in imported.sqlalchemy_uri
    assert "XXXXXXXXXX" in imported.sqlalchemy_uri
    assert imported.password == "supersecret"


async def test_dataset_import_database_prevent_unsafe(import_env):
    """high: PREVENT_UNSAFE_DB_CONNECTIONS rejects sqlite URIs on import."""
    from superset.exceptions import ImportFailedError

    session, _db, _user = import_env
    cmd = _make_dataset_cmd(session)

    with pytest.raises(ImportFailedError):
        await cmd._import_database(
            "databases/evil.yaml",
            {
                "uuid": "dddddddd-0000-0000-0000-000000000003",
                "database_name": "evil_db",
                "sqlalchemy_uri": "sqlite:////etc/passwd",
                "extra": {},
            },
        )


async def test_dataset_import_database_calls_add_permissions(
    import_env, add_permissions_calls
):
    """high: catalog/schema DAR permissions are granted for the imported DB."""
    session, _db, _user = import_env
    cmd = _make_dataset_cmd(session)

    await cmd._import_database(
        "databases/imported.yaml",
        {
            "uuid": "dddddddd-0000-0000-0000-000000000004",
            "database_name": "perm_db",
            "sqlalchemy_uri": "postgresql://user:pw@localhost:5432/db",
            "extra": {},
        },
    )

    assert len(add_permissions_calls) == 1
    assert add_permissions_calls[0].database_name == "perm_db"


# --------------------------------------------------------------------------- #
# _import_chart overwrite-permission signatures (crit)
# --------------------------------------------------------------------------- #


class _StubSecurityManager:
    """Mimics AsyncSecurityManager's exact signatures (keyword-only user)."""

    def __init__(
        self,
        can_access: bool = True,
        chart_access: bool = True,
        admin: bool = False,
    ) -> None:
        self._can_access = can_access
        self._chart_access = chart_access
        self._admin = admin

    async def can_access(self, permission_name, view_name, *, user):
        return self._can_access

    async def can_access_chart(self, chart, *, user):
        return self._chart_access

    def is_admin(self, user):
        return self._admin


def _chart_config(uuid_str: str) -> dict:
    return {
        "uuid": uuid_str,
        "slice_name": "imported chart",
        "viz_type": "table",
        "params": {},
        "datasource_id": 1,
        "datasource_type": "table",
    }


async def test_import_chart_overwrite_denied_clean_403(import_env):
    """Non-owner, non-admin without chart access -> ImportFailedError.

    Pre-fix: TypeError (can_access_chart() missing keyword-only 'user' /
    await on the non-async is_admin()) -> HTTP 500.
    """
    from superset.commands.chart.importers.v1.utils import _import_chart
    from superset.exceptions import ImportFailedError
    from superset.models.slice import Slice

    session, _db, user = import_env
    existing = Slice(
        slice_name="old chart",
        viz_type="table",
        params="{}",
        datasource_id=1,
        datasource_type="table",
        uuid="eeeeeeee-0000-0000-0000-000000000001",
        owners=[],
    )
    session.add(existing)
    await session.commit()

    sm = _StubSecurityManager(can_access=True, chart_access=False, admin=False)
    with pytest.raises(ImportFailedError):
        await _import_chart(
            session,
            _chart_config("eeeeeeee-0000-0000-0000-000000000001"),
            overwrite=True,
            security_manager=sm,
            current_user=user,
        )


async def test_import_chart_overwrite_allowed_for_admin(import_env):
    """Admin overwrite proceeds without TypeError and updates the chart."""
    from superset.commands.chart.importers.v1.utils import _import_chart
    from superset.models.slice import Slice

    session, _db, user = import_env
    existing = Slice(
        slice_name="old chart",
        viz_type="table",
        params="{}",
        datasource_id=1,
        datasource_type="table",
        uuid="eeeeeeee-0000-0000-0000-000000000002",
        owners=[],
    )
    session.add(existing)
    await session.commit()

    sm = _StubSecurityManager(can_access=True, chart_access=True, admin=True)
    chart = await _import_chart(
        session,
        _chart_config("eeeeeeee-0000-0000-0000-000000000002"),
        overwrite=True,
        security_manager=sm,
        current_user=user,
    )
    assert chart.id == existing.id
    assert chart.slice_name == "imported chart"


async def test_import_chart_bundle_schema_validates_each_entry():
    """Per-entry schema validation (load_configs parity): a bundle whose chart
    YAML has a valid ``slice_name`` (so the subclass ``_validate`` passes) but
    is missing the schema-required ``uuid`` must still be rejected with a
    field-keyed 422 — proving the base ``validate()`` runs the ImportV1Chart
    schema on every entry, not just the targeted slice_name check."""
    import io
    import zipfile

    import yaml

    from superset.commands.chart.importers.v1 import ImportChartsCommand
    from superset.exceptions import CommandInvalidError

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "bundle/metadata.yaml",
            yaml.safe_dump({"version": "1.0.0", "type": "Slice"}),
        )
        zf.writestr(
            "bundle/charts/bad.yaml",
            # valid slice_name + viz_type, but no uuid / version / dataset_uuid
            yaml.safe_dump({"slice_name": "ok name", "viz_type": "table"}),
        )
    buf.seek(0)

    cmd = ImportChartsCommand(contents=buf, dao=None)
    with pytest.raises(CommandInvalidError) as exc_info:
        await cmd.validate()
    # The structured payload keys the failure by file name and lists uuid.
    errors = getattr(exc_info.value, "extra", {}).get("errors", {})
    assert "charts/bad.yaml" in errors
    assert "uuid" in errors["charts/bad.yaml"]
