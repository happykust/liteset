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
"""Flask-free port of ``tests/integration_tests/security_tests.py``.

Exercises the async :class:`AsyncSecurityManager` (``superset.security.manager``),
the PVM predicate helpers (``superset.security.sync_roles``) and the guest-token
helpers (``superset.security.guest``) against the REAL seeded Postgres backend.

Several upstream behaviours have no 1:1 equivalent in the Litestar port and are
skipped with a factual reason:

* The ``test_after_insert/update/delete_*`` and ``test_set_perm_slice`` family
  relies on Flask-AppBuilder SQLAlchemy *event listeners* that auto-create /
  rename / delete permission rows on ``db.session.commit()``. The port has no
  model event listeners — permission sync is driven explicitly by the command
  layer via ``AsyncPermissionManager`` (``superset.security.permission_manager``).
* ``test_*_permissions`` / ``test_sql_lab_permissions`` assert the exact set of
  ``(permission, view_menu)`` tuples produced by FAB *view registration*. The
  port's permission catalogue is the curated ``sync_roles`` static list, which
  intentionally omits FAB-view artefacts (``can_view_chart_as_table``,
  ``can_explore_json``, granular ``TabStateView`` perms, ``TableSchemaView`` …),
  so the upstream tuples are structurally absent.
* ``test_views_are_secured`` inspects ``appbuilder.baseviews`` (Flask-only).
* ``TestDatasources`` tests ``get_user_datasources`` which the port replaced with
  ``filter_datasources_by_perms`` / ``get_datasources_accessible_by_user``.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from sqlalchemy import select

from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import SupersetSecurityException
from superset.models.connectors import SqlaTable
from superset.models.core import Database
from superset.models.security import Permission, PermissionView, ViewMenu
from superset.security.dao import AsyncSecurityDAO
from superset.security.guest import (
    create_guest_access_token,
    GuestUser,
    parse_guest_token,
)
from superset.security.manager import AsyncSecurityManager
from superset.security.sync_roles import (
    _is_admin_only,
    _is_alpha_only,
    _is_gamma_pvm,
)
from superset.sql.parse import Table
from tests.superset.integration import factories as f

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager(db_session: Any, **kwargs: Any) -> AsyncSecurityManager:
    """Build an ``AsyncSecurityManager`` bound to the real async session."""
    return AsyncSecurityManager(dao=AsyncSecurityDAO(db_session), **kwargs)


async def _grant_pvms(db_session: Any, role: Any, *pvms: Any) -> None:
    """Append PVMs to ``role.permissions`` without tripping a sync lazy-load.

    ``Role.permissions`` is ``lazy="select"``; touching it on a role bound to
    the async session fires synchronous IO (MissingGreenlet). Refresh the
    collection through the async session first so the subsequent append/read is
    a no-op DB-wise.
    """
    await db_session.refresh(role, attribute_names=["permissions"])
    for pvm in pvms:
        role.permissions.append(pvm)
    await db_session.flush()


async def _create_group(
    db_session: Any, name: str, roles: list[Any]
) -> Any:
    """Persist an ``ab_group`` row with the given roles (port of add_group)."""
    from superset.models.security import Group

    group = Group(name=name)
    # Pre-init the lazy="select" collection so the subsequent append/assign does
    # not fire a synchronous lazy-load on the async session.
    group.roles = []
    db_session.add(group)
    await db_session.flush()
    for role in roles:
        group.roles.append(role)
    await db_session.flush()
    return group


async def _create_user_in_group(
    db_session: Any, username: str, group: Any
) -> Any:
    """Persist a user whose only role membership is via ``group``.

    ``groups`` is set on the transient instance at construction so the
    assignment never fires a synchronous lazy-load on the async session.
    """
    user = await f.create_user(
        db_session, username=username, roles=[], groups=[group]
    )
    return user


def _datasource_mock() -> Any:
    """Port of ``SupersetTestCase.get_datasource_mock``.

    A MagicMock that quacks like a ``SqlaTable`` with ``perm`` / ``schema_perm``
    set so permission checks have something to read.
    """
    datasource = MagicMock()
    datasource.type = "table"
    datasource.database = MagicMock()
    datasource.database.db_engine_spec = MagicMock()
    datasource.database.perm = "mock_database_perm"
    datasource.schema_perm = "mock_schema_perm"
    datasource.perm = "mock_datasource_perm"
    datasource.__class__ = SqlaTable
    datasource.owners = []
    datasource.id = 99999
    return datasource


async def _get_birth_names(db_session: Any) -> SqlaTable:
    result = await db_session.execute(
        select(SqlaTable).where(SqlaTable.table_name == "birth_names")
    )
    return result.scalars().first()


async def _get_example_database(db_session: Any) -> Database:
    result = await db_session.execute(
        select(Database).where(Database.database_name == "examples")
    )
    return result.scalars().first()


# ---------------------------------------------------------------------------
# PVM predicate helpers (port of test_is_admin_only / _alpha_only / _gamma_pvm)
# ---------------------------------------------------------------------------


def _build_pvm(permission_name: str, view_menu_name: str) -> PermissionView:
    """Build a detached ``PermissionView`` graph for predicate testing."""
    return PermissionView(
        permission=Permission(name=permission_name),
        view_menu=ViewMenu(name=view_menu_name),
    )


def test_is_admin_only() -> None:
    # can_read on a data model is NOT admin-only
    assert not _is_admin_only(_build_pvm("can_read", "Dataset"))
    # all_datasource_access is an object-spec / data perm, not admin-only
    assert not _is_admin_only(
        _build_pvm("all_datasource_access", "all_datasource_access")
    )
    # Log views are admin-only
    assert _is_admin_only(_build_pvm("can_read", "Log"))
    # User management is admin-only
    assert _is_admin_only(_build_pvm("can_edit", "UserDBModelView"))


def test_is_alpha_only() -> None:
    # Read on a dataset is available below alpha (gamma reads datasets)
    assert not _is_alpha_only(_build_pvm("can_read", "Dataset"))
    # Write on a dataset is alpha-only
    assert _is_alpha_only(_build_pvm("can_write", "Dataset"))
    # all_datasource_access / all_database_access are alpha-only
    assert _is_alpha_only(
        _build_pvm("all_datasource_access", "all_datasource_access")
    )
    assert _is_alpha_only(
        _build_pvm("all_database_access", "all_database_access")
    )


def test_is_gamma_pvm() -> None:
    assert _is_gamma_pvm(_build_pvm("can_read", "Dataset"))


# ---------------------------------------------------------------------------
# get_schemas_accessible_by_user
# ---------------------------------------------------------------------------


async def test_schemas_accessible_by_user_admin(db_session: Any) -> None:
    sm = _make_manager(db_session)
    admin_role = await f.create_role(db_session, name="Admin")
    admin = await f.create_user(
        db_session, username="sec_admin", roles=[admin_role]
    )
    database = await _get_example_database(db_session)
    schemas = await sm.get_schemas_accessible_by_user(
        database, ["1", "2", "3"], user=admin
    )
    assert set(schemas) == {"1", "2", "3"}  # no changes for admin


async def test_schemas_accessible_by_user_schema_access(db_session: Any) -> None:
    """A gamma user with schema_access to [examples].[1] sees only schema 1."""
    sm = _make_manager(db_session)
    database = await _get_example_database(db_session)
    schema_perm = f"[examples].[1]"  # noqa: F541
    # Build the schema_access PVM and grant it to a fresh role.
    pvm = await sm.add_permission_view_menu("schema_access", schema_perm)
    role = await f.create_role(db_session, name="schema_access_role")
    await _grant_pvms(db_session, role, pvm)
    user = await f.create_user(db_session, username="sec_gamma", roles=[role])

    schemas = await sm.get_schemas_accessible_by_user(
        database, ["1", "2", "3"], user=user
    )
    assert set(schemas) == {"1"}


async def test_schemas_accessible_by_user_datasource_access(
    db_session: Any,
) -> None:
    """schema access inferred from a datasource_access permission."""
    sm = _make_manager(db_session)
    database = await _get_example_database(db_session)
    # Create a dataset in a known schema and grant datasource_access to it.
    dataset = await f.create_dataset(
        db_session,
        table_name="sec_temp_table",
        database=database,
        schema="temp_schema",
        perm="[examples].[sec_temp_table](id:1)",
    )
    dataset.perm = "[examples].[sec_temp_table](id:1)"
    await db_session.flush()
    pvm = await sm.add_permission_view_menu("datasource_access", dataset.perm)
    role = await f.create_role(db_session, name="ds_access_role")
    await _grant_pvms(db_session, role, pvm)
    user = await f.create_user(db_session, username="sec_ds", roles=[role])

    schemas = await sm.get_schemas_accessible_by_user(
        database, ["temp_schema", "2", "3"], user=user
    )
    assert set(schemas) == {"temp_schema"}


async def test_schemas_accessible_by_user_datasource_and_schema_access(
    db_session: Any,
) -> None:
    sm = _make_manager(db_session)
    database = await _get_example_database(db_session)
    dataset = await f.create_dataset(
        db_session,
        table_name="sec_temp_table2",
        database=database,
        schema="temp_schema",
        perm="[examples].[sec_temp_table2](id:1)",
    )
    dataset.perm = "[examples].[sec_temp_table2](id:1)"
    await db_session.flush()
    role = await f.create_role(db_session, name="ds_schema_role")
    ds_pvm = await sm.add_permission_view_menu("datasource_access", dataset.perm)
    schema_pvm = await sm.add_permission_view_menu("schema_access", "[examples].[2]")
    await _grant_pvms(db_session, role, ds_pvm, schema_pvm)
    user = await f.create_user(
        db_session, username="sec_ds_schema", roles=[role]
    )

    schemas = await sm.get_schemas_accessible_by_user(
        database, ["temp_schema", "2", "3"], user=user
    )
    assert set(schemas) == {"temp_schema", "2"}
    vm = await sm.find_permission_view_menu("schema_access", "[examples].[2]")
    assert vm is not None


# ---------------------------------------------------------------------------
# TestSecurityManager: can_access_* + raise_for_access
# ---------------------------------------------------------------------------


async def _gamma_user(db_session: Any) -> Any:
    role = await f.create_role(db_session, name="Gamma")
    return await f.create_user(db_session, username="raise_gamma", roles=[role])


async def test_can_access_datasource(db_session: Any) -> None:
    sm = _make_manager(db_session)
    user = await _gamma_user(db_session)
    datasource = _datasource_mock()

    # can_access_datasource inlines its own checks (it does NOT delegate to
    # raise_for_access in the port); drive it by patching the access primitives.
    with patch.object(sm, "is_admin", return_value=False), patch.object(
        sm, "has_access", new=AsyncMock(return_value=True)
    ):
        assert await sm.can_access_datasource(datasource, user=user) is True

    with patch.object(sm, "is_admin", return_value=False), patch.object(
        sm, "has_access", new=AsyncMock(return_value=False)
    ), patch.object(
        sm, "_can_access_datasource_schema", new=AsyncMock(return_value=False)
    ), patch.object(sm, "is_owner", return_value=False):
        assert await sm.can_access_datasource(datasource, user=user) is False


async def test_can_access_table(db_session: Any) -> None:
    sm = _make_manager(db_session)
    user = await _gamma_user(db_session)
    database = await _get_example_database(db_session)
    table = Table("bar", "foo")

    with patch.object(sm, "raise_for_access", new=AsyncMock(return_value=None)):
        assert await sm.can_access_table(database, table, user=user) is True

    with patch.object(
        sm,
        "raise_for_access",
        new=AsyncMock(
            side_effect=SupersetSecurityException(
                SupersetError(
                    "dummy",
                    SupersetErrorType.TABLE_SECURITY_ACCESS_ERROR,
                    ErrorLevel.ERROR,
                )
            )
        ),
    ):
        assert await sm.can_access_table(database, table, user=user) is False


async def test_raise_for_access_datasource(db_session: Any) -> None:
    sm = _make_manager(db_session)
    user = await _gamma_user(db_session)
    datasource = _datasource_mock()

    with patch.object(
        sm, "_can_access_datasource_schema", new=AsyncMock(return_value=True)
    ):
        await sm.raise_for_access(datasource=datasource, user=user)

    with patch.object(
        sm, "_can_access_datasource_schema", new=AsyncMock(return_value=False)
    ), patch.object(sm, "can_access", new=AsyncMock(return_value=False)), patch.object(
        sm, "is_owner", return_value=False
    ):
        with pytest.raises(SupersetSecurityException):
            await sm.raise_for_access(datasource=datasource, user=user)


async def test_raise_for_access_query(db_session: Any) -> None:
    sm = _make_manager(db_session)
    user = await _gamma_user(db_session)
    database = await _get_example_database(db_session)
    query = MagicMock(
        database=database,
        schema="bar",
        sql="SELECT * FROM foo",
        catalog=None,
    )

    with patch.object(sm, "can_access_database", new=AsyncMock(return_value=True)):
        await sm.raise_for_access(query=query, user=user)

    with patch.object(
        sm, "can_access_database", new=AsyncMock(return_value=False)
    ), patch.object(sm, "can_access", new=AsyncMock(return_value=False)), patch.object(
        sm, "is_owner", return_value=False
    ), patch.object(
        sm, "_can_access_table_datasource", new=AsyncMock(return_value=False)
    ):
        with pytest.raises(SupersetSecurityException):
            await sm.raise_for_access(query=query, user=user)


async def test_raise_for_access_sql_fails(db_session: Any) -> None:
    sm = _make_manager(db_session)
    user = await _gamma_user(db_session)
    database = await _get_example_database(db_session)
    with pytest.raises(SupersetSecurityException):
        await sm.raise_for_access(
            database=database,
            schema="bar",
            sql="SELECT * FROM foo",
            user=user,
        )


async def test_raise_for_access_sql(db_session: Any) -> None:
    sm = _make_manager(db_session)
    user = await _gamma_user(db_session)
    database = await _get_example_database(db_session)
    with patch.object(sm, "can_access_database", new=AsyncMock(return_value=True)):
        await sm.raise_for_access(
            database=database,
            schema="bar",
            sql="SELECT * FROM foo",
            user=user,
        )


async def test_raise_for_access_query_context(db_session: Any) -> None:
    sm = _make_manager(db_session)
    user = await _gamma_user(db_session)
    query_context = MagicMock(datasource=_datasource_mock(), form_data={})

    with patch.object(
        sm, "_can_access_datasource_schema", new=AsyncMock(return_value=True)
    ):
        await sm.raise_for_access(query_context=query_context, user=user)

    with patch.object(
        sm, "_can_access_datasource_schema", new=AsyncMock(return_value=False)
    ), patch.object(sm, "can_access", new=AsyncMock(return_value=False)), patch.object(
        sm, "is_owner", return_value=False
    ):
        with pytest.raises(SupersetSecurityException):
            await sm.raise_for_access(query_context=query_context, user=user)


async def test_raise_for_access_table(db_session: Any) -> None:
    sm = _make_manager(db_session)
    user = await _gamma_user(db_session)
    database = await _get_example_database(db_session)
    table = Table("bar", "foo")

    with patch.object(sm, "can_access_database", new=AsyncMock(return_value=True)):
        await sm.raise_for_access(database=database, table=table, user=user)

    with patch.object(
        sm, "can_access_database", new=AsyncMock(return_value=False)
    ), patch.object(sm, "can_access", new=AsyncMock(return_value=False)), patch.object(
        sm, "_can_access_table_datasource", new=AsyncMock(return_value=False)
    ):
        with pytest.raises(SupersetSecurityException):
            await sm.raise_for_access(database=database, table=table, user=user)


async def test_raise_for_access_viz(db_session: Any) -> None:
    sm = _make_manager(db_session)
    user = await _gamma_user(db_session)
    test_viz = MagicMock(datasource=_datasource_mock(), form_data={})

    with patch.object(
        sm, "_can_access_datasource_schema", new=AsyncMock(return_value=True)
    ):
        await sm.raise_for_access(viz=test_viz, user=user)

    with patch.object(
        sm, "_can_access_datasource_schema", new=AsyncMock(return_value=False)
    ), patch.object(sm, "can_access", new=AsyncMock(return_value=False)), patch.object(
        sm, "is_owner", return_value=False
    ):
        with pytest.raises(SupersetSecurityException):
            await sm.raise_for_access(viz=test_viz, user=user)


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@pytest.mark.usefixtures("load_world_bank_dashboard_with_slices")
async def test_raise_for_access_rbac(db_session: Any) -> None:
    """DASHBOARD_RBAC fallback in ``raise_for_access`` for query_context + viz.

    Mirrors the upstream matrix: when a gamma user lacks direct datasource
    access, access is only granted when the dashboard RBAC role matches AND the
    chart / native-filter resource genuinely belongs to the dashboard +
    datasource.
    """
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice
    from superset.utils import json

    sm = _make_manager(db_session, dashboard_rbac_enabled=True)

    gamma_role = await f.create_role(db_session, name="Gamma")
    user = await f.create_user(db_session, username="rbac_gamma", roles=[gamma_role])

    births = (
        await db_session.execute(select(Dashboard).where(Dashboard.slug == "births"))
    ).scalars().first()
    world_health = (
        await db_session.execute(
            select(Dashboard).where(Dashboard.slug == "world_health")
        )
    ).scalars().first()
    girls = (
        await db_session.execute(select(Slice).where(Slice.slice_name == "Girls"))
    ).scalars().first()
    treemap = (
        await db_session.execute(select(Slice).where(Slice.slice_name == "Treemap"))
    ).scalars().first()
    # Eager-load datasources so the sync ``.datasource`` reads don't fire a
    # lazy-load on the async session.
    await db_session.refresh(girls, attribute_names=["table"])
    await db_session.refresh(treemap, attribute_names=["table"])
    # Pre-load ``births.roles`` so the ``births.roles = [...]`` mutations below
    # don't fire a sync lazy-load on the async session.
    await db_session.refresh(births, attribute_names=["roles"])
    birth_names = girls.datasource

    births.json_metadata = json.dumps(
        {
            "native_filter_configuration": [
                {
                    "id": "NATIVE_FILTER-ABCDEFGH",
                    "targets": [{"datasetId": birth_names.id}],
                },
                {
                    "id": "NATIVE_FILTER-IJKLMNOP",
                    "targets": [{"datasetId": treemap.datasource.id}],
                },
            ]
        }
    )
    await db_session.flush()

    with patch.object(sm, "is_owner", return_value=False), patch.object(
        sm, "can_access", new=AsyncMock(return_value=False)
    ), patch.object(
        sm, "_can_access_datasource_schema", new=AsyncMock(return_value=False)
    ), patch.object(
        sm, "_can_access_table_datasource", new=AsyncMock(return_value=False)
    ), patch.object(sm, "can_access_dashboard", new=AsyncMock(return_value=True)):
        for kwarg in ["query_context", "viz"]:
            births.roles = []
            await db_session.flush()

            # No dashboard roles -> denied.
            with pytest.raises(SupersetSecurityException):
                await sm.raise_for_access(
                    user=user,
                    **{
                        kwarg: MagicMock(
                            datasource=birth_names,
                            form_data={
                                "dashboardId": births.id,
                                "slice_id": girls.id,
                            },
                        )
                    },
                )

            births.roles = [gamma_role]
            await db_session.flush()

            # Undefined dashboard.
            with pytest.raises(SupersetSecurityException):
                await sm.raise_for_access(
                    user=user,
                    **{kwarg: MagicMock(datasource=birth_names, form_data={})},
                )

            # Undefined dashboard chart.
            with pytest.raises(SupersetSecurityException):
                await sm.raise_for_access(
                    user=user,
                    **{
                        kwarg: MagicMock(
                            datasource=birth_names,
                            form_data={"dashboardId": births.id},
                        )
                    },
                )

            # Ill-defined dashboard chart (chart not on this dashboard).
            with pytest.raises(SupersetSecurityException):
                await sm.raise_for_access(
                    user=user,
                    **{
                        kwarg: MagicMock(
                            datasource=birth_names,
                            form_data={
                                "dashboardId": births.id,
                                "slice_id": treemap.id,
                            },
                        )
                    },
                )

            # Dashboard chart not associated with said datasource.
            with pytest.raises(SupersetSecurityException):
                await sm.raise_for_access(
                    user=user,
                    **{
                        kwarg: MagicMock(
                            datasource=birth_names,
                            form_data={
                                "dashboardId": world_health.id,
                                "slice_id": treemap.id,
                            },
                        )
                    },
                )

            # Dashboard chart associated with said datasource -> allowed.
            await sm.raise_for_access(
                user=user,
                **{
                    kwarg: MagicMock(
                        datasource=birth_names,
                        form_data={
                            "dashboardId": births.id,
                            "slice_id": girls.id,
                        },
                    )
                },
            )

            # Ill-defined native filter.
            with pytest.raises(SupersetSecurityException):
                await sm.raise_for_access(
                    user=user,
                    **{
                        kwarg: MagicMock(
                            datasource=birth_names,
                            form_data={
                                "dashboardId": births.id,
                                "type": "NATIVE_FILTER",
                            },
                        )
                    },
                )

            # Native filter not associated with said datasource.
            with pytest.raises(SupersetSecurityException):
                await sm.raise_for_access(
                    user=user,
                    **{
                        kwarg: MagicMock(
                            datasource=birth_names,
                            form_data={
                                "dashboardId": births.id,
                                "native_filter_id": "NATIVE_FILTER-IJKLMNOP",
                                "type": "NATIVE_FILTER",
                            },
                        )
                    },
                )

            # Native filter associated with said datasource -> allowed.
            await sm.raise_for_access(
                user=user,
                **{
                    kwarg: MagicMock(
                        datasource=birth_names,
                        form_data={
                            "dashboardId": births.id,
                            "native_filter_id": "NATIVE_FILTER-ABCDEFGH",
                            "type": "NATIVE_FILTER",
                        },
                    )
                },
            )


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@pytest.mark.usefixtures("load_world_bank_dashboard_with_slices")
async def test_gamma_user_schema_access_to_dashboards(db_session: Any) -> None:
    """Port of upstream ``test_gamma_user_schema_access_to_dashboards``.

    A gamma user whose only data grant is ``datasource_access`` to the
    ``world_health`` dashboard's datasource sees ONLY ``world_health`` (not
    ``births``) when the dashboard list is restricted by the RBAC base filter.

    Upstream asserts this through ``GET /api/v1/dashboard/`` (world_health in
    the response body, births absent). The port's equivalent of that
    per-request resource filtering is :func:`dashboard_access_filters`, the
    base filter the dashboard ``get_list`` handler prepends to every query
    (``superset/controllers/dashboard.py``). Exercising that real seam against
    the seeded Postgres reproduces the upstream visibility guarantee 1:1.
    """
    from superset.db.filters import dashboard_access_filters
    from superset.models.dashboard import Dashboard

    sm = _make_manager(db_session)

    # wb_health_population (id:2) backs every ``world_health`` slice; granting
    # only its datasource_access perm mirrors upstream moving that dataset into
    # a dedicated schema and granting schema_access to just that schema.
    role = await f.create_role(db_session, name="schema_access_role")
    pvm = await sm.add_permission_view_menu(
        "datasource_access", "[examples].[wb_health_population](id:2)"
    )
    await _grant_pvms(db_session, role, pvm)
    user = await f.create_user(
        db_session, username="schema_gamma", roles=[role]
    )

    base_filters = await dashboard_access_filters(sm, user)
    assert base_filters  # non-admin -> restrictive filter, never empty
    stmt = select(Dashboard.slug)
    for clause in base_filters:
        stmt = stmt.where(clause)
    visible = set((await db_session.execute(stmt)).scalars().all())

    assert "world_health" in visible
    assert "births" not in visible


@pytest.mark.usefixtures("load_world_bank_dashboard_with_slices")
async def test_sqllab_gamma_user_schema_access_to_sqllab(db_session: Any) -> None:
    """Port of upstream ``test_sqllab_gamma_user_schema_access_to_sqllab``.

    A gamma user with ``schema_access`` to ``[examples].[public]`` sees exactly
    one ``expose_in_sqllab`` database (``examples``) when the database list is
    restricted by the RBAC base filter. Upstream asserts ``count == 1`` from
    ``GET /api/v1/database/?q=...expose_in_sqllab=eq:True``; the port's
    equivalent base filter is :func:`database_access_filters`
    (``superset/controllers/database.py``). A gamma user WITHOUT the grant sees
    zero, confirming the count is driven by the RBAC filter and not merely by
    the number of seeded databases.
    """
    from superset.db.filters import database_access_filters

    sm = _make_manager(db_session)

    examples = await _get_example_database(db_session)
    examples.expose_in_sqllab = True
    await db_session.flush()

    role = await f.create_role(db_session, name="sqllab_schema_role")
    pvm = await sm.add_permission_view_menu("schema_access", "[examples].[public]")
    await _grant_pvms(db_session, role, pvm)
    user = await f.create_user(
        db_session, username="sqllab_schema_gamma", roles=[role]
    )

    async def _expose_in_sqllab_count(target_user: Any) -> int:
        base_filters = await database_access_filters(sm, target_user)
        stmt = select(Database.id).where(Database.expose_in_sqllab.is_(True))
        for clause in base_filters:
            stmt = stmt.where(clause)
        return len((await db_session.execute(stmt)).scalars().all())

    assert await _expose_in_sqllab_count(user) == 1

    # A gamma user without the schema grant is filtered down to zero databases,
    # proving the count is the RBAC filter result, not the raw table size.
    no_grant_role = await f.create_role(db_session, name="sqllab_no_grant_role")
    no_grant_user = await f.create_user(
        db_session, username="sqllab_no_grant_gamma", roles=[no_grant_role]
    )
    assert await _expose_in_sqllab_count(no_grant_user) == 0


async def test_get_admin_user_roles(db_session: Any) -> None:
    sm = _make_manager(db_session)
    admin_role = await f.create_role(db_session, name="Admin")
    admin = await f.create_user(db_session, username="roles_admin", roles=[admin_role])
    roles = await sm.get_user_roles(admin)
    assert admin.roles == roles


async def test_get_gamma_user_roles(db_session: Any) -> None:
    sm = _make_manager(db_session)
    gamma_role = await f.create_role(db_session, name="Gamma")
    gamma = await f.create_user(db_session, username="roles_gamma", roles=[gamma_role])
    roles = await sm.get_user_roles(gamma)
    assert gamma.roles == roles


async def test_get_user_roles_with_groups(db_session: Any) -> None:
    """A user with no direct roles inherits its group's roles.

    Port of upstream ``test_get_user_roles_with_groups``: upstream asserts
    ``user.groups[0].roles == get_user_roles()``. The port wraps
    group-inherited roles in ``_GroupRoleRef`` (id/name), so compare by the
    resolved role names instead of object identity.
    """
    sm = _make_manager(db_session)
    gamma_role = await f.create_role(db_session, name="Gamma")
    group = await _create_group(db_session, "group1", [gamma_role])
    user = await _create_user_in_group(db_session, "gamma_with_groups", group)

    roles = await sm.get_user_roles(user)
    assert [r.name for r in group.roles] == [r.name for r in roles]


async def test_get_user_roles_with_groups_dar(db_session: Any) -> None:
    """Group roles are merged across multiple roles on the same group.

    Port of upstream ``test_get_user_roles_with_groups_dar``.
    """
    sm = _make_manager(db_session)
    gamma_role = await f.create_role(db_session, name="Gamma")
    dar_role = await f.create_role(db_session, name="dar")
    dar_pvm = await sm.add_permission_view_menu(
        "datasource_access", "[examples].[birth_names](id:1)]"
    )
    await _grant_pvms(db_session, dar_role, dar_pvm)
    group = await _create_group(db_session, "group1", [dar_role, gamma_role])
    user = await _create_user_in_group(db_session, "gamma_with_groups", group)

    role_names = [role.name for role in await sm.get_user_roles(user)]
    assert "Gamma" in role_names
    assert "dar" in role_names
    assert len(role_names) == 2


async def test_user_view_menu_names_with_groups_dar(db_session: Any) -> None:
    """``user_view_menu_names`` resolves a PVM inherited only via a group role.

    Port of upstream ``test_user_view_menu_names_with_groups_dar``.
    """
    sm = _make_manager(db_session)
    dar_role = await f.create_role(db_session, name="dar")
    dar_pvm = await sm.add_permission_view_menu(
        "datasource_access", "[examples].[birth_names](id:1)]"
    )
    await _grant_pvms(db_session, dar_role, dar_pvm)
    group = await _create_group(db_session, "group1", [dar_role])
    user = await _create_user_in_group(db_session, "gamma_with_groups", group)

    assert await sm.user_view_menu_names("datasource_access", user=user) == {
        "[examples].[birth_names](id:1)]"
    }


async def test_gamma_user_view_menu_names_with_groups_dar(db_session: Any) -> None:
    """View-menu names are resolved across all group-inherited roles.

    Port of upstream ``test_gamma_user_view_menu_names_with_groups_dar``: the dar
    role contributes a ``datasource_access`` PVM and the gamma role contributes
    ``can_external_metadata``/``Datasource`` and ``can_recent_activity``/``Log``
    PVMs; all must surface through ``user_view_menu_names`` via the group.
    """
    sm = _make_manager(db_session)
    gamma_role = await f.create_role(db_session, name="Gamma")
    dar_role = await f.create_role(db_session, name="dar")
    dar_pvm = await sm.add_permission_view_menu(
        "datasource_access", "[examples].[birth_names](id:1)]"
    )
    await _grant_pvms(db_session, dar_role, dar_pvm)
    # The seeded Gamma role in this DB does not carry the curated Gamma
    # permission catalogue; grant the exact PVMs the upstream Gamma role holds
    # so the group view-menu resolution can be asserted 1:1.
    metadata_pvm = await sm.add_permission_view_menu(
        "can_external_metadata", "Datasource"
    )
    log_pvm = await sm.add_permission_view_menu("can_recent_activity", "Log")
    await _grant_pvms(db_session, gamma_role, metadata_pvm, log_pvm)
    group = await _create_group(db_session, "group1", [dar_role, gamma_role])
    user = await _create_user_in_group(db_session, "gamma_with_groups", group)

    # assert pvm for dar role
    assert await sm.user_view_menu_names("datasource_access", user=user) == {
        "[examples].[birth_names](id:1)]"
    }
    # assert pvm for gamma role
    assert await sm.user_view_menu_names("can_external_metadata", user=user) == {
        "Datasource"
    }
    assert await sm.user_view_menu_names("can_recent_activity", user=user) == {
        "Log"
    }


async def test_all_database_access(db_session: Any) -> None:
    sm = _make_manager(db_session)
    gamma_role = await f.create_role(db_session, name="Gamma")
    gamma_user = await f.create_user(
        db_session, username="alldb_gamma", roles=[gamma_role]
    )
    datasource = _datasource_mock()

    # Double check that gamma users can't access all databases.
    assert not await sm.can_access_all_databases(user=gamma_user)
    assert not await sm.can_access_datasource(datasource, user=gamma_user)

    # Grant all_database_access to the gamma role and re-check.
    pvm = await sm.add_permission_view_menu(
        "all_database_access", "all_database_access"
    )
    await _grant_pvms(db_session, gamma_role, pvm)

    # ``has_access`` resolves perms via the DAO keyed on the user's role ids
    # (already loaded on ``gamma_user.roles``); the freshly-granted PVM is in
    # the DB after the flush above, so both checks now pass.
    assert await sm.can_access_all_databases(user=gamma_user)
    assert await sm.can_access_datasource(datasource, user=gamma_user)


# ---------------------------------------------------------------------------
# Guest tokens (ported to the Litestar guest-token API surface)
# ---------------------------------------------------------------------------

GUEST_SECRET = "guest-token-secret-key-at-least-32-bytes"


def test_create_guest_access_token() -> None:
    now = int(time.time())
    user = {"username": "test_guest"}
    resources = [{"some": "resource"}]
    rls = [{"dataset": 1, "clause": "access = 1"}]
    audience = "http://localhost:8088/"
    exp_seconds = 300

    token = create_guest_access_token(
        secret_key=GUEST_SECRET,
        user=user,
        resources=resources,
        rls=rls,
        exp_seconds=exp_seconds,
        audience=audience,
    )
    decoded_token = jwt.decode(
        token,
        GUEST_SECRET,
        algorithms=["HS256"],
        audience=audience,
    )

    assert user == decoded_token["user"]
    assert resources == decoded_token["resources"]
    # Upstream mocks _get_current_epoch_time and asserts ``now == iat``; the port
    # computes ``iat`` internally via int(time.time()) with no injection point, so
    # the exact equality relaxes to ``iat >= now``. The exp relationship below is
    # still asserted exactly relative to the token's own iat.
    assert decoded_token["iat"] >= now
    assert audience == decoded_token["aud"]
    assert "guest" == decoded_token["type"]
    assert decoded_token["iat"] + exp_seconds == decoded_token["exp"]


def test_parse_guest_token_roundtrip() -> None:
    user = {"username": "test_guest"}
    resources = [{"type": "dashboard", "id": 1}]
    rls = [{"dataset": 1, "clause": "access = 1"}]
    token = create_guest_access_token(
        secret_key=GUEST_SECRET, user=user, resources=resources, rls=rls
    )
    payload = parse_guest_token(token, GUEST_SECRET)
    assert payload is not None
    guest_user = GuestUser.from_token_payload(payload)
    assert guest_user is not None
    assert "test_guest" == guest_user.username
    assert guest_user.is_guest is True
    assert resources == guest_user.resources
    assert rls == guest_user.rls_rules


def test_parse_guest_token_expired() -> None:
    user = {"username": "test_guest"}
    resources = [{"type": "dashboard", "id": 1}]
    # Negative exp_seconds yields an already-expired token.
    token = create_guest_access_token(
        secret_key=GUEST_SECRET,
        user=user,
        resources=resources,
        rls=[],
        exp_seconds=-10,
    )
    assert parse_guest_token(token, GUEST_SECRET) is None


def test_parse_guest_token_not_guest_type() -> None:
    now = int(time.time())
    claims = {
        "user": {"username": "test_guest"},
        "resources": [{"some": "resource"}],
        "rls_rules": [],
        "aud": "",
        "iat": now,
        "exp": now + 300,
        "type": "not_guest",
    }
    token = jwt.encode(claims, GUEST_SECRET, algorithm="HS256")
    # Token type mismatch -> parse returns None (matches upstream "not a guest").
    assert parse_guest_token(token, GUEST_SECRET) is None


def test_parse_guest_token_bad_audience() -> None:
    token = create_guest_access_token(
        secret_key=GUEST_SECRET,
        user={"username": "test_guest"},
        resources=[{"some": "resource"}],
        rls=[],
        audience="good_audience",
    )
    # Decoding with a different expected audience must fail the InvalidAudience
    # check and return None.
    assert parse_guest_token(token, GUEST_SECRET, audience="bad_audience") is None


def test_parse_guest_token_bad_secret() -> None:
    token = create_guest_access_token(
        secret_key=GUEST_SECRET,
        user={"username": "test_guest"},
        resources=[{"some": "resource"}],
        rls=[],
    )
    assert parse_guest_token(token, "a-totally-different-secret-key-xxxxxx") is None


# ---------------------------------------------------------------------------
# Guest-token claim validation (port of test_get_guest_user_no_user /
# test_get_guest_user_no_resource).
#
# In the Litestar port the missing-claim validation lives in the auth
# middleware's ``_resolve_guest_from_jwt`` (superset/middleware/auth.py): a
# token whose ``user`` or ``resources`` claim is absent resolves to no guest
# user (returns ``None``). ``parse_guest_token`` itself only validates the
# token ``type``, so the claim-validation paths are exercised through the
# middleware method directly.
# ---------------------------------------------------------------------------


def _guest_settings() -> Any:
    """Minimal settings stub for ``_resolve_guest_from_jwt``."""
    settings = MagicMock()
    settings.guest_token_jwt_secret = GUEST_SECRET
    settings.guest_token_jwt_algo = "HS256"
    settings.guest_token_jwt_audience = None
    settings.webdriver_baseurl = ""
    settings.guest_role_name = "Guest"
    return settings


def _guest_connection(session_factory: Any | None = None) -> Any:
    """Build a connection stub carrying the guest settings + session factory."""
    connection = MagicMock()
    connection.app.state.settings = _guest_settings()
    connection.app.state.session_factory = session_factory
    return connection


async def test_get_guest_user_no_user() -> None:
    """A token with a null ``user`` claim resolves to no guest user."""
    from superset.middleware.auth import SupersetAuthMiddleware

    now = int(time.time())
    claims = {
        "user": None,
        "resources": [{"type": "dashboard", "id": 1}],
        "rls_rules": [],
        "aud": "",
        "iat": now,
        "exp": now + 300,
        "type": "guest",
    }
    token = jwt.encode(claims, GUEST_SECRET, algorithm="HS256")

    middleware = SupersetAuthMiddleware.__new__(SupersetAuthMiddleware)
    guest_user = await middleware._resolve_guest_from_jwt(
        _guest_connection(), token
    )
    assert guest_user is None


async def test_get_guest_user_no_resource() -> None:
    """A token with a null ``resources`` claim resolves to no guest user."""
    from superset.middleware.auth import SupersetAuthMiddleware

    now = int(time.time())
    claims = {
        "user": {"username": "test_guest"},
        "resources": None,
        "rls_rules": [],
        "aud": "",
        "iat": now,
        "exp": now + 300,
        "type": "guest",
    }
    token = jwt.encode(claims, GUEST_SECRET, algorithm="HS256")

    middleware = SupersetAuthMiddleware.__new__(SupersetAuthMiddleware)
    guest_user = await middleware._resolve_guest_from_jwt(
        _guest_connection(), token
    )
    assert guest_user is None


# ---------------------------------------------------------------------------
# Structurally-absent upstream behaviour — skipped with factual reasons.
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="Port's public create_guest_access_token takes a plain audience:str; "
    "callable GUEST_TOKEN_JWT_AUDIENCE resolution moved to the auth middleware "
    "(_resolve_guest_from_jwt resolves a callable config value before signing), "
    "so the token-creation function never invokes a callable itself. Covers "
    "upstream test_create_guest_access_token_callable_audience."
)
def test_create_guest_access_token_callable_audience() -> None:
    """Upstream: a callable GUEST_TOKEN_JWT_AUDIENCE is invoked and its result
    becomes the 'aud' claim of the created token.

        self.app.config["GUEST_TOKEN_JWT_AUDIENCE"] = Mock(return_value="cool_code")
        token = security_manager.create_guest_access_token(user, resources, rls)
        decoded_token = jwt.decode(token, ..., audience="cool_code")
        self.app.config["GUEST_TOKEN_JWT_AUDIENCE"].assert_called_once()
        assert "cool_code" == decoded_token["aud"]
        assert "guest" == decoded_token["type"]
    """


@pytest.mark.skip(
    reason="Port has no FAB SQLAlchemy event listeners: dataset/database "
    "permission creation/rename/delete is driven explicitly by the command "
    "layer via AsyncPermissionManager, not by db.session.commit() hooks."
)
def test_after_insert_update_delete_permission_listeners() -> None:
    """Covers the upstream test_after_insert/update/delete_* family."""


@pytest.mark.skip(
    reason="Port has no set_perm event listener: Slice.perm/SqlaTable.perm are "
    "not auto-populated on commit (covers test_set_perm_slice/test_hybrid_perm)."
)
def test_set_perm_slice() -> None:
    """Covers test_set_perm_slice / test_hybrid_perm_database."""


@pytest.mark.skip(
    reason="Port's permission catalogue is the curated sync_roles static list, "
    "not the FAB view-registration set; exact upstream (perm, view_menu) tuples "
    "(can_view_chart_as_table, can_explore_json, granular TabStateView perms, "
    "TableSchemaView, …) are structurally absent. Covers test_*_permissions / "
    "test_sql_lab_permissions only. The gamma schema-access-to-dashboards / "
    "-sqllab resource-filtering behaviour IS ported above against the real RBAC "
    "base filters (test_gamma_user_schema_access_to_dashboards / "
    "test_sqllab_gamma_user_schema_access_to_sqllab), and public_sync_role_data_"
    "perms is covered in tests/superset/unit/test_sync_roles.py."
)
def test_role_permission_tuples() -> None:
    """Covers the role/permission-tuple assertion family."""


@pytest.mark.skip(
    reason="test_views_are_secured inspects Flask-AppBuilder appbuilder.baseviews; "
    "the port has no FAB view registry."
)
def test_views_are_secured() -> None:
    pass


@pytest.mark.skip(
    reason="Port replaced get_user_datasources with filter_datasources_by_perms / "
    "get_datasources_accessible_by_user (perm-string based, no SqlaTable."
    "get_all_datasources classmethod). Covers TestDatasources.*"
)
def test_get_user_datasources() -> None:
    pass


@pytest.mark.skip(
    reason="Port's get_anonymous_user returns UnauthenticatedUser(roles=[]); "
    "get_user_roles does not auto-inject the Public role for anonymous users "
    "(that resolution lives in _user_permission_pairs / user_view_menu_names)."
)
def test_get_anonymous_roles() -> None:
    pass


@pytest.mark.skip(
    reason="get_guest_user_from_request in the port reads request.user set by the "
    "auth middleware; it does not decode a GUEST_TOKEN header off a FakeRequest "
    "from app config. Header decoding is covered by test_async_token_middleware."
)
def test_get_guest_user_from_request_header() -> None:
    pass
