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
"""Utilities for validating datasource and chart access in explore/permalink flows.

Provides ``check_access(datasource_id, chart_id, datasource_type)`` —
used by :class:`CreateExplorePermalinkCommand`,
:class:`GetExplorePermalinkCommand`, :class:`CreateFormDataCommand`, and
:class:`UpdateFormDataCommand`.

Raises one of:
* ``ObjectNotFoundError`` / ``ForbiddenError`` for datasets
* ``SupersetGenericErrorException`` (status=400) for queries and invalid types
* ``ChartNotFoundError`` / ``ForbiddenError`` for charts
"""

from __future__ import annotations

from typing import Any

from superset.exceptions import (
    ForbiddenError,
    ObjectNotFoundError,
    SupersetGenericErrorException,
)


async def check_dataset_access(
    dataset_dao: Any,
    dataset_id: int,
    *,
    security_manager: Any,
    user: Any,
) -> bool:
    """Ensure the current user can access dataset ``dataset_id``.

    Raises ``ObjectNotFoundError`` when the dataset is missing,
    ``ForbiddenError`` when the user lacks ``can_access_datasource``.
    """
    if not dataset_id:
        raise ObjectNotFoundError("Dataset", dataset_id)

    # Eager-load owners + database so can_access_datasource can read M2M and
    # schema-access relationships without triggering a sync lazy-load on the
    # async session (raises MissingGreenlet for users lacking all_datasource_access).
    from sqlalchemy.orm import selectinload

    from superset.models.connectors import SqlaTable

    if hasattr(dataset_dao, "find_by_id_with_options"):
        dataset = await dataset_dao.find_by_id_with_options(
            dataset_id,
            [selectinload(SqlaTable.owners), selectinload(SqlaTable.database)],
        )
    else:
        dataset = await dataset_dao.find_by_id(dataset_id)
    if dataset is None:
        raise ObjectNotFoundError("Dataset", dataset_id)

    can_access_datasource = await security_manager.can_access_datasource(
        dataset, user=user
    )
    if can_access_datasource:
        return True
    raise ForbiddenError(
        f"User has no access to datasource {dataset_id}",
    )


async def check_query_access(
    query_dao: Any,
    query_id: int,
    *,
    security_manager: Any,
    user: Any,
) -> bool:
    """Ensure the current user can access query ``query_id``."""
    if not query_id:
        raise SupersetGenericErrorException(f"Missing query id: {query_id}", status=400)

    query = await query_dao.find_by_id(query_id)
    if query is None:
        raise SupersetGenericErrorException(f"Query {query_id} not found", status=400)

    await security_manager.raise_for_access(query=query, user=user)
    return True


async def check_datasource_access(
    *,
    datasource_id: int,
    datasource_type: str,
    dataset_dao: Any,
    query_dao: Any,
    security_manager: Any,
    user: Any,
) -> bool:
    """Dispatch ``check_dataset_access`` or ``check_query_access`` based on
    ``datasource_type``.
    """
    if not datasource_id:
        raise SupersetGenericErrorException("Missing datasource id", status=400)

    if datasource_type == "table":
        return await check_dataset_access(
            dataset_dao,
            datasource_id,
            security_manager=security_manager,
            user=user,
        )
    if datasource_type == "query":
        return await check_query_access(
            query_dao,
            datasource_id,
            security_manager=security_manager,
            user=user,
        )
    raise SupersetGenericErrorException(
        f"Invalid datasource type: {datasource_type}", status=400
    )


async def check_access(
    *,
    datasource_id: int,
    chart_id: int | None,
    datasource_type: str,
    dataset_dao: Any,
    query_dao: Any,
    chart_dao: Any,
    security_manager: Any,
    user: Any,
) -> bool:
    """Validate datasource and optional chart access for the current user.

    1. Confirm the user can access ``datasource_id``.
    2. If ``chart_id`` is provided, confirm that the user is either an
       owner of the chart or holds the ``can_read Chart`` permission.

    Raises ``ForbiddenError`` / ``ObjectNotFoundError`` on failure.
    """
    await check_datasource_access(
        datasource_id=datasource_id,
        datasource_type=datasource_type,
        dataset_dao=dataset_dao,
        query_dao=query_dao,
        security_manager=security_manager,
        user=user,
    )

    if not chart_id:
        return True

    # Eager-load owners so is_owner can read the M2M without triggering a sync
    # lazy-load on the async session (raises MissingGreenlet).
    from sqlalchemy.orm import selectinload

    from superset.models.slice import Slice

    chart = await chart_dao.find_by_id_with_options(
        chart_id, [selectinload(Slice.owners)]
    )
    if chart is None:
        # ChartNotFoundError is distinct from ObjectNotFoundError — permalink GET
        # wraps only dataset-not-found into a 500; chart-not-found surfaces as 404.
        from superset.exceptions import ChartNotFoundError

        raise ChartNotFoundError()

    is_owner = security_manager.is_owner(chart, user)
    can_read_chart = await security_manager.can_access("can_read", "Chart", user=user)
    if is_owner or can_read_chart:
        return True
    raise ForbiddenError(f"User has no access to chart {chart_id}")
