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
"""Async port of ``superset_old/explore/utils.py``.

Provides ``check_access(datasource_id, chart_id, datasource_type)`` —
the same signature surface used by the original
``CreateExplorePermalinkCommand`` / ``GetExplorePermalinkCommand`` /
``CreateFormDataCommand`` / ``UpdateFormDataCommand``.

The original raises one of:

* ``DatasetAccessDeniedError`` / ``DatasetNotFoundError``
* ``QueryNotFoundValidationError``
* ``DatasourceNotFoundValidationError`` / ``DatasourceTypeInvalidError``
* ``ChartAccessDeniedError`` / ``ChartNotFoundError``

The async port keeps the same control flow but maps to the existing
Liteset exceptions so the controllers can catch them generically.
"""

from __future__ import annotations

from typing import Any

from superset.exceptions import CommandInvalidError, ForbiddenError, ObjectNotFoundError


async def check_dataset_access(
    dataset_dao: Any,
    dataset_id: int,
    *,
    security_manager: Any,
    user: Any,
) -> bool:
    """Ensure the current user can access dataset ``dataset_id``.

    1:1 with ``superset_old/explore/utils.py:check_dataset_access``:
    raises ``ObjectNotFoundError`` when the dataset is missing,
    ``ForbiddenError`` when the user lacks ``can_access_datasource``.
    """
    if not dataset_id:
        raise ObjectNotFoundError("Dataset", dataset_id)

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
    """Ensure the current user can access query ``query_id``.

    1:1 with ``superset_old/explore/utils.py:check_query_access``.
    """
    if not query_id:
        raise CommandInvalidError(f"Missing query id: {query_id}")

    query = await query_dao.find_by_id(query_id)
    if query is None:
        raise CommandInvalidError(f"Query {query_id} not found")

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
    """Dispatch ``check_dataset_access`` / ``check_query_access`` based on
    ``datasource_type``.  Mirrors the original ``ACCESS_FUNCTION_MAP``
    table.
    """
    if not datasource_id:
        raise CommandInvalidError("Missing datasource id")

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
    raise CommandInvalidError(f"Invalid datasource type: {datasource_type}")


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
    """1:1 port of ``superset_old/explore/utils.py:check_access``.

    1. Validate that the current user can access ``datasource_id``.
    2. If ``chart_id`` is provided, confirm that the user is either an
       owner of the chart or holds the global ``can_read Chart`` perm.

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

    chart = await chart_dao.find_by_id(chart_id)
    if chart is None:
        raise ObjectNotFoundError("Chart", chart_id)

    is_owner = security_manager.is_owner(chart, user)
    can_read_chart = await security_manager.can_access(
        "can_read", "Chart", user=user
    )
    if is_owner or can_read_chart:
        return True
    raise ForbiddenError(f"User has no access to chart {chart_id}")
