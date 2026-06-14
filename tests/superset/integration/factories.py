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

"""Model factories for Flask-free integration tests.

These replace the upstream ``load_birth_names`` / ``load_world_bank``
example-data fixtures: instead of seeding a Postgres example database, each
factory builds the minimal real ORM object and persists it through the
provided ``AsyncSession`` (see ``db_session`` in ``conftest.py``).

Convention: every factory is async, takes the session as its first argument,
``add``s + ``flush``es the object (so its autoincrement id is populated), and
returns it. Parent rows are flushed before children so the children can set
their foreign key by id without appending to a lazy="select" collection on a
transient parent (which would fire a sync SELECT).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from superset.models.connectors import SqlaTable, SqlMetric, TableColumn
from superset.models.core import Database
from superset.models.dashboard import Dashboard
from superset.models.security import Role, User
from superset.models.slice import Slice

_SQLITE_URI = "sqlite://"


async def create_user(
    session: AsyncSession,
    *,
    username: str = "admin",
    first_name: str = "admin",
    last_name: str = "user",
    email: str | None = None,
    roles: list[Role] | None = None,
    **kwargs: Any,
) -> User:
    """Persist a :class:`User`."""
    user = User(
        username=username,
        first_name=first_name,
        last_name=last_name,
        email=email or f"{username}@example.com",
        **kwargs,
    )
    if roles is not None:
        user.roles = roles
    session.add(user)
    await session.flush()
    return user


async def create_role(
    session: AsyncSession, *, name: str = "Admin", **kwargs: Any
) -> Role:
    """Persist a :class:`Role`."""
    role = Role(name=name, **kwargs)
    session.add(role)
    await session.flush()
    return role


async def create_database(
    session: AsyncSession,
    *,
    database_name: str = "test_db",
    sqlalchemy_uri: str = _SQLITE_URI,
    **kwargs: Any,
) -> Database:
    """Persist a :class:`Database`."""
    database = Database(
        database_name=database_name,
        sqlalchemy_uri=sqlalchemy_uri,
        **kwargs,
    )
    session.add(database)
    await session.flush()
    return database


async def create_column(
    session: AsyncSession,
    table_id: int,
    *,
    column_name: str,
    type: str = "VARCHAR",
    is_dttm: bool = False,
    groupby: bool = True,
    **kwargs: Any,
) -> TableColumn:
    """Persist a :class:`TableColumn` for an existing dataset id."""
    column = TableColumn(
        table_id=table_id,
        column_name=column_name,
        type=type,
        is_dttm=is_dttm,
        groupby=groupby,
        **kwargs,
    )
    session.add(column)
    await session.flush()
    return column


async def create_metric(
    session: AsyncSession,
    table_id: int,
    *,
    metric_name: str = "count",
    expression: str = "COUNT(*)",
    **kwargs: Any,
) -> SqlMetric:
    """Persist a :class:`SqlMetric` for an existing dataset id."""
    metric = SqlMetric(
        table_id=table_id,
        metric_name=metric_name,
        expression=expression,
        **kwargs,
    )
    session.add(metric)
    await session.flush()
    return metric


async def create_dataset(
    session: AsyncSession,
    *,
    table_name: str = "test_table",
    database: Database | None = None,
    columns: list[dict[str, Any]] | None = None,
    metrics: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> SqlaTable:
    """Persist a :class:`SqlaTable`, optionally with columns and metrics.

    ``columns`` / ``metrics`` are lists of kwargs dicts forwarded to
    :func:`create_column` / :func:`create_metric`.
    """
    if database is None:
        database = await create_database(session)
    dataset = SqlaTable(
        table_name=table_name,
        database_id=database.id,
        **kwargs,
    )
    session.add(dataset)
    await session.flush()
    for col in columns or []:
        await create_column(session, dataset.id, **col)
    for metric in metrics or []:
        await create_metric(session, dataset.id, **metric)
    return dataset


async def create_chart(
    session: AsyncSession,
    *,
    slice_name: str = "test_chart",
    viz_type: str = "table",
    datasource_id: int | None = None,
    datasource_type: str = "table",
    params: str = "{}",
    **kwargs: Any,
) -> Slice:
    """Persist a :class:`Slice`."""
    chart = Slice(
        slice_name=slice_name,
        viz_type=viz_type,
        datasource_id=datasource_id,
        datasource_type=datasource_type,
        params=params,
        **kwargs,
    )
    session.add(chart)
    await session.flush()
    return chart


async def create_dashboard(
    session: AsyncSession,
    *,
    dashboard_title: str = "test_dashboard",
    slug: str | None = None,
    published: bool = False,
    **kwargs: Any,
) -> Dashboard:
    """Persist a :class:`Dashboard`."""
    dashboard = Dashboard(
        dashboard_title=dashboard_title,
        slug=slug,
        published=published,
        **kwargs,
    )
    session.add(dashboard)
    await session.flush()
    return dashboard
