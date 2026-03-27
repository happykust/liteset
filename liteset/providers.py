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
"""Centralized DAO provider functions for Litestar DI.

All DAO providers use lazy imports to avoid triggering the Flask
import chain from superset/ at module load time.

NOTE: Return type is `Any` intentionally. Litestar's DI resolves
dependencies by parameter name, not by type annotation. Using concrete
DAO types (e.g. AsyncChartDAO) in return annotations would force the
import at module level, pulling in superset models and the Flask init
chain. This will be resolved in Phase 7 (cleanup) when superset model
imports are no longer needed.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


def provide_chart_dao(session: AsyncSession) -> Any:
    from liteset.db.daos.chart import AsyncChartDAO

    return AsyncChartDAO(session)


def provide_dashboard_dao(session: AsyncSession) -> Any:
    from liteset.db.daos.dashboard import AsyncDashboardDAO

    return AsyncDashboardDAO(session)


def provide_embedded_dao(session: AsyncSession) -> Any:
    from liteset.db.daos.dashboard import AsyncEmbeddedDashboardDAO

    return AsyncEmbeddedDashboardDAO(session)


def provide_database_dao(session: AsyncSession) -> Any:
    from liteset.db.daos.database import AsyncDatabaseDAO

    return AsyncDatabaseDAO(session)


def provide_dataset_dao(session: AsyncSession) -> Any:
    from liteset.db.daos.dataset import AsyncDatasetDAO

    return AsyncDatasetDAO(session)


def provide_column_dao(session: AsyncSession) -> Any:
    from liteset.db.daos.dataset import AsyncDatasetColumnDAO

    return AsyncDatasetColumnDAO(session)


def provide_metric_dao(session: AsyncSession) -> Any:
    from liteset.db.daos.dataset import AsyncDatasetMetricDAO

    return AsyncDatasetMetricDAO(session)


def provide_query_dao(session: AsyncSession) -> Any:
    from liteset.db.daos.query import AsyncQueryDAO

    return AsyncQueryDAO(session)


def provide_saved_query_dao(session: AsyncSession) -> Any:
    from liteset.db.daos.query import AsyncSavedQueryDAO

    return AsyncSavedQueryDAO(session)


def provide_kv_dao(session: AsyncSession) -> Any:
    from liteset.db.daos.key_value import AsyncKeyValueDAO

    return AsyncKeyValueDAO(session)


def provide_datasource_dao(session: AsyncSession) -> Any:
    from liteset.db.daos.datasource import AsyncDatasourceDAO

    return AsyncDatasourceDAO(session)


def provide_role_dao(session: AsyncSession) -> Any:
    from liteset.db.daos.security import AsyncRoleDAO

    return AsyncRoleDAO(session)


def provide_css_template_dao(session: AsyncSession) -> Any:
    from liteset.db.daos.css import AsyncCssTemplateDAO

    return AsyncCssTemplateDAO(session)


def provide_annotation_layer_dao(session: AsyncSession) -> Any:
    from liteset.db.daos.annotation import AsyncAnnotationLayerDAO

    return AsyncAnnotationLayerDAO(session)


def provide_annotation_dao(session: AsyncSession) -> Any:
    from liteset.db.daos.annotation import AsyncAnnotationDAO

    return AsyncAnnotationDAO(session)


def provide_log_dao(session: AsyncSession) -> Any:
    from liteset.db.daos.log import AsyncLogDAO

    return AsyncLogDAO(session)


def provide_report_dao(session: AsyncSession) -> Any:
    from liteset.db.daos.report import AsyncReportScheduleDAO

    return AsyncReportScheduleDAO(session)


def provide_report_execution_log_dao(session: AsyncSession) -> Any:
    from liteset.db.daos.report import AsyncReportExecutionLogDAO

    return AsyncReportExecutionLogDAO(session)


def provide_tag_dao(session: AsyncSession) -> Any:
    from liteset.db.daos.tag import AsyncTagDAO

    return AsyncTagDAO(session)


def provide_theme_dao(session: AsyncSession) -> Any:
    from liteset.db.daos.theme import AsyncThemeDAO

    return AsyncThemeDAO(session)


def provide_rls_dao(session: AsyncSession) -> Any:
    from liteset.db.base_dao import BaseAsyncDAO
    from liteset.models.connectors import RowLevelSecurityFilter

    class AsyncRLSDAO(BaseAsyncDAO[RowLevelSecurityFilter]):
        model_cls = RowLevelSecurityFilter

    return AsyncRLSDAO(session)
