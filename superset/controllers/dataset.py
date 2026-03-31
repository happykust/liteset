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
"""Dataset controller — 13 endpoints for dataset CRUD,
export/import, duplicate, refresh."""

from __future__ import annotations

import io
from typing import Any

import msgspec
from litestar import Controller, delete, get, post, put
from litestar.datastructures import UploadFile
from litestar.di import Provide
from litestar.enums import RequestEncodingType
from litestar.params import Body, Parameter
from litestar.response import Stream

from superset.commands.dataset import (
    BulkDeleteDatasetsCommand,
    CreateDatasetCommand,
    DeleteDatasetColumnCommand,
    DeleteDatasetCommand,
    DeleteDatasetMetricCommand,
    DuplicateDatasetCommand,
    ExportDatasetsCommand,
    GetOrCreateDatasetCommand,
    ImportDatasetsCommand,
    RefreshDatasetCommand,
    UpdateDatasetCommand,
    WarmUpDatasetCacheCommand,
)

# DAO imports moved to provider functions
from superset.controllers.base import (
    build_export_headers,
    extract_ids,
    extract_ids_required,
    extract_pagination,
    get_distinct_payload,
    get_info_payload,
    get_related_payload,
    serialize_list_response,
    stream_zip,
)
from superset.events import event_logger
from superset.exceptions import CommandInvalidError, ObjectNotFoundError
from superset.guards.rbac import require_permission
from superset.params.rison import provide_rison_query
from superset.providers import (
    provide_column_dao,
    provide_dataset_dao,
    provide_metric_dao,
)
from superset.schemas.dataset import (
    DatasetCacheWarmUpRequest,
    DatasetDuplicateSchema,
    DatasetGetResponse,
    DatasetListResponse,
    DatasetPostSchema,
    DatasetPutSchema,
    GetOrCreateDatasetSchema,
)
from superset.typing import (
    ColumnDAOProtocol,
    DatasetDAOProtocol,
    MetricDAOProtocol,
    SecurityManagerProtocol,
    UserProtocol,
)
from superset.utils import filter_none, filter_unset


def _build_dataset_result(dataset: Any) -> dict[str, Any]:
    """Build expanded dataset result dict for create/update responses.

    Returns key fields so the frontend gets a useful response beyond
    just ``table_name``, matching the original Superset contract.
    """
    database = getattr(dataset, "database", None)
    changed_on = getattr(dataset, "changed_on", None)
    created_on = getattr(dataset, "created_on", None)
    return {
        "table_name": dataset.table_name,
        "schema": getattr(dataset, "schema", None),
        "sql": getattr(dataset, "sql", None),
        "database_id": getattr(dataset, "database_id", None),
        "uuid": str(dataset.uuid) if getattr(dataset, "uuid", None) else None,
        "description": getattr(dataset, "description", None),
        "cache_timeout": getattr(dataset, "cache_timeout", None),
        "main_dttm_col": getattr(dataset, "main_dttm_col", None),
        "datasource_type": getattr(dataset, "datasource_type", "table"),
        "created_on": created_on.isoformat() if created_on else None,
        "changed_on": changed_on.isoformat() if changed_on else None,
        "database": {
            "id": database.id,
            "database_name": getattr(database, "database_name", ""),
        }
        if database
        else None,
    }


class DatasetController(Controller):
    path = "/api/v1/dataset"
    tags = ["Datasets"]
    dependencies = {
        "dao": Provide(provide_dataset_dao, sync_to_thread=False),
        "column_dao": Provide(provide_column_dao, sync_to_thread=False),
        "metric_dao": Provide(provide_metric_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    @get(
        "/",
        guards=[require_permission("can_read", "Dataset")],
    )
    async def get_list(
        self,
        dao: DatasetDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        rison_params: dict[str, Any] | None,
    ) -> DatasetListResponse:
        from superset.db.filters import dataset_access_filters

        page, page_size = extract_pagination(rison_params)
        base_filters = await dataset_access_filters(security_manager, current_user)
        datasets = await dao.find_all(
            filters=base_filters or None, page=page, page_size=page_size
        )
        total = await dao.count(filters=base_filters or None)
        event_logger.log("dataset.list")
        payload = serialize_list_response(
            datasets,
            total,
            ["id", "table_name", "schema", "database_id"],
        )
        return DatasetListResponse(result=payload["result"], count=payload["count"])

    @get(
        "/_info",
        guards=[require_permission("can_read", "Dataset")],
    )
    async def info(self, dao: DatasetDAOProtocol) -> dict[str, Any]:
        """GET /api/v1/dataset/_info — API metadata for frontend."""
        return await get_info_payload(
            dao=dao,
            model_name="Dataset",
            permissions=["can_read", "can_write"],
        )

    @get(
        "/related/{column_name:str}",
        guards=[require_permission("can_read", "Dataset")],
    )
    async def related(
        self,
        column_name: str,
        dao: DatasetDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/dataset/related/{column_name}"""
        from superset.db.filters import dataset_access_filters

        base_filters = await dataset_access_filters(security_manager, current_user)
        return await get_related_payload(
            dao=dao,
            column_name=column_name,
            rison_params=rison_params,
            allowed_fields=frozenset(
                {"database", "owners", "created_by", "changed_by"}
            ),
            base_filters=base_filters or None,
        )

    @get(
        "/distinct/{column_name:str}",
        guards=[require_permission("can_read", "Dataset")],
    )
    async def distinct(
        self,
        column_name: str,
        dao: DatasetDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/dataset/distinct/{column_name}"""
        from superset.db.filters import dataset_access_filters

        base_filters = await dataset_access_filters(security_manager, current_user)
        return await get_distinct_payload(
            dao=dao,
            column_name=column_name,
            rison_params=rison_params,
            allowed_fields=frozenset({"catalog", "schema"}),
            base_filters=base_filters or None,
        )

    @get(
        "/{pk:int}",
        guards=[require_permission("can_read", "Dataset")],
    )
    async def get_dataset(
        self,
        pk: int,
        dao: DatasetDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        include_rendered_sql: bool = Parameter(
            query="include_rendered_sql", default=False,
        ),
    ) -> DatasetGetResponse:
        dataset = await dao.find_by_id(pk)
        if not dataset:
            raise ObjectNotFoundError("Dataset", pk)
        # Verify object-level access
        from superset.db.filters import dataset_access_filters

        base_filters = await dataset_access_filters(security_manager, current_user)
        if base_filters:
            from sqlalchemy import select as sa_select

            model_cls = getattr(dao, "model_cls", None)
            if model_cls is not None:
                stmt = sa_select(model_cls.id).where(
                    model_cls.id == dataset.id, *base_filters
                )
                result = await dao.session.scalar(stmt)
                if result is None:
                    raise ObjectNotFoundError("Dataset", pk)
        columns = getattr(dataset, "columns", []) or []
        metrics = getattr(dataset, "metrics", []) or []
        owners = getattr(dataset, "owners", []) or []
        database = getattr(dataset, "database", None)
        changed_on = getattr(dataset, "changed_on", None)
        created_on = getattr(dataset, "created_on", None)

        raw_sql = getattr(dataset, "sql", None)
        rendered_sql: str | None = None
        if include_rendered_sql and raw_sql:
            # Attempt Jinja rendering of the SQL template.  Falls back
            # to the raw SQL string when the template engine is not
            # available or rendering fails.
            try:
                from superset.jinja_context import get_template_processor

                tp = get_template_processor(
                    database=database, table=dataset
                )
                rendered_sql = tp.process_template(raw_sql)
            except Exception:  # noqa: BLE001
                rendered_sql = raw_sql

        result_dict: dict[str, Any] = {
            "table_name": dataset.table_name,
            "schema": getattr(dataset, "schema", None),
            "sql": raw_sql,
            "description": getattr(dataset, "description", None),
            "cache_timeout": dataset.cache_timeout,
            "uuid": str(dataset.uuid) if getattr(dataset, "uuid", None) else None,
            "main_dttm_col": getattr(dataset, "main_dttm_col", None),
            "template_params": getattr(dataset, "template_params", None),
            "datasource_type": getattr(dataset, "datasource_type", "table"),
            "kind": getattr(dataset, "kind", None),
            "created_on": created_on.isoformat() if created_on else None,
            "changed_on": changed_on.isoformat() if changed_on else None,
            "database": {
                "id": database.id,
                "database_name": getattr(database, "database_name", ""),
            }
            if database
            else None,
            "owners": [{"id": o.id, "name": str(o)} for o in owners],
            "columns": [
                {
                    "id": getattr(col, "id", None),
                    "column_name": getattr(col, "column_name", ""),
                    "verbose_name": getattr(col, "verbose_name", None),
                    "description": getattr(col, "description", None),
                    "expression": getattr(col, "expression", None),
                    "type": getattr(col, "type", None),
                    "type_generic": getattr(col, "type_generic", None),
                    "python_date_format": getattr(col, "python_date_format", None),
                    "is_dttm": getattr(col, "is_dttm", False),
                    "is_active": getattr(col, "is_active", True),
                    "groupby": getattr(col, "groupby", True),
                    "filterable": getattr(col, "filterable", True),
                    "uuid": (
                        str(getattr(col, "uuid", None))
                        if getattr(col, "uuid", None)
                        else None
                    ),
                    "advanced_data_type": getattr(col, "advanced_data_type", None),
                    "extra": getattr(col, "extra", None),
                }
                for col in columns
            ],
            "metrics": [
                {
                    "id": getattr(m, "id", None),
                    "metric_name": getattr(m, "metric_name", ""),
                    "verbose_name": getattr(m, "verbose_name", None),
                    "description": getattr(m, "description", None),
                    "expression": getattr(m, "expression", ""),
                    "metric_type": getattr(m, "metric_type", None),
                    "d3format": getattr(m, "d3format", None),
                    "currency": getattr(m, "currency", None),
                    "warning_text": getattr(m, "warning_text", None),
                    "extra": getattr(m, "extra", None),
                    "uuid": (
                        str(getattr(m, "uuid", None))
                        if getattr(m, "uuid", None)
                        else None
                    ),
                }
                for m in metrics
            ],
        }

        if include_rendered_sql:
            result_dict["rendered_sql"] = rendered_sql

        return DatasetGetResponse(
            id=dataset.id,
            result=result_dict,
        )

    @post(
        "/",
        guards=[require_permission("can_write", "Dataset")],
        status_code=201,
    )
    async def create(
        self,
        data: DatasetPostSchema,
        dao: DatasetDAOProtocol,
        current_user: UserProtocol,
    ) -> DatasetGetResponse:
        cmd = CreateDatasetCommand(
            dao=dao,
            data=filter_none(
                {
                    "table_name": data.table_name,
                    "database": data.database,
                    "schema_name": data.schema_name,
                    "sql": data.sql,
                    "is_managed_externally": data.is_managed_externally,
                    "external_url": data.external_url,
                    "normalize_columns": data.normalize_columns,
                    "always_filter_main_dttm": data.always_filter_main_dttm,
                }
            ),
            user_id=current_user.id,
        )
        dataset = await cmd.execute()
        event_logger.log(
            "dataset.create",
            object_ref=f"dataset:{dataset.id}",
            user_id=current_user.id,
        )
        return DatasetGetResponse(
            id=dataset.id,
            result=_build_dataset_result(dataset),
        )

    @put(
        "/{pk:int}",
        guards=[require_permission("can_write", "Dataset")],
    )
    async def update(
        self,
        pk: int,
        data: DatasetPutSchema,
        dao: DatasetDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        override_columns: bool = Parameter(query="override_columns", default=False),
    ) -> DatasetGetResponse:
        update_data: dict[str, Any] = filter_unset(
            {
                "table_name": data.table_name,
                "database_id": data.database_id,
                "sql": data.sql,
                "schema_name": data.schema_name,
                "description": data.description,
                "main_dttm_col": data.main_dttm_col,
                "offset": data.offset,
                "default_endpoint": data.default_endpoint,
                "cache_timeout": data.cache_timeout,
                "is_sqllab_view": data.is_sqllab_view,
                "template_params": data.template_params,
                "extra": data.extra,
                "is_managed_externally": data.is_managed_externally,
                "external_url": data.external_url,
                "normalize_columns": data.normalize_columns,
                "always_filter_main_dttm": data.always_filter_main_dttm,
            }
        )
        if data.columns is not msgspec.UNSET and data.columns is not None:
            update_data["columns"] = [
                {k: v for k, v in msgspec.structs.asdict(c).items() if v is not None}
                for c in data.columns
            ]
        if data.metrics is not msgspec.UNSET and data.metrics is not None:
            update_data["metrics"] = [
                {k: v for k, v in msgspec.structs.asdict(m).items() if v is not None}
                for m in data.metrics
            ]

        # When override_columns is True, refresh columns from the database
        # instead of applying user-provided column changes.
        if override_columns:
            cmd_refresh = RefreshDatasetCommand(
                dao=dao,
                dataset_id=pk,
                security_manager=security_manager,
                user_id=current_user.id,
            )
            dataset = await cmd_refresh.execute()
        else:
            cmd = UpdateDatasetCommand(
                dao=dao,
                dataset_id=pk,
                data=update_data,
                user_id=current_user.id,
                security_manager=security_manager,
            )
            dataset = await cmd.execute()

        event_logger.log(
            "dataset.update",
            object_ref=f"dataset:{pk}",
            user_id=current_user.id,
        )
        return DatasetGetResponse(
            id=dataset.id,
            result=_build_dataset_result(dataset),
        )

    @delete(
        "/{pk:int}",
        guards=[require_permission("can_write", "Dataset")],
        status_code=200,
    )
    async def delete_dataset(
        self,
        pk: int,
        dao: DatasetDAOProtocol,
        security_manager: "SecurityManagerProtocol",
        current_user: UserProtocol,
    ) -> dict[str, str]:
        cmd = DeleteDatasetCommand(
            dao=dao,
            dataset_id=pk,
            security_manager=security_manager,
            user_id=current_user.id,
        )
        await cmd.execute()
        event_logger.log(
            "dataset.delete",
            object_ref=f"dataset:{pk}",
            user_id=current_user.id,
        )
        return {"message": "OK"}

    @delete(
        "/",
        guards=[require_permission("can_write", "Dataset")],
        status_code=200,
    )
    async def bulk_delete(
        self,
        dao: DatasetDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, str]:
        ids = extract_ids_required(rison_params)
        cmd = BulkDeleteDatasetsCommand(
            dao=dao,
            dataset_ids=ids,
            security_manager=security_manager,
            user_id=current_user.id,
        )
        await cmd.execute()
        event_logger.log(
            "dataset.bulk_delete",
            user_id=current_user.id,
            extra={"count": len(ids)},
        )
        return {"message": "OK"}

    @post(
        "/duplicate",
        guards=[require_permission("can_write", "Dataset")],
        status_code=201,
    )
    async def duplicate(
        self,
        data: DatasetDuplicateSchema,
        dao: DatasetDAOProtocol,
        current_user: UserProtocol,
    ) -> DatasetGetResponse:
        cmd = DuplicateDatasetCommand(
            dao=dao,
            base_model_id=data.base_model_id,
            table_name=data.table_name,
            user_id=current_user.id,
        )
        dataset = await cmd.execute()
        event_logger.log(
            "dataset.duplicate",
            object_ref=f"dataset:{dataset.id}",
            user_id=current_user.id,
        )
        return DatasetGetResponse(
            id=dataset.id,
            result={"table_name": dataset.table_name},
        )

    @put(
        "/{pk:int}/refresh",
        guards=[require_permission("can_write", "Dataset")],
    )
    async def refresh(self, pk: int, dao: DatasetDAOProtocol) -> dict[str, str]:
        cmd = RefreshDatasetCommand(dao=dao, dataset_id=pk)
        await cmd.execute()
        event_logger.log("dataset.refresh", object_ref=f"dataset:{pk}")
        return {"message": "OK"}

    @post(
        "/get_or_create/",
        guards=[require_permission("can_write", "Dataset")],
        status_code=200,
    )
    async def get_or_create(
        self,
        data: GetOrCreateDatasetSchema,
        dao: DatasetDAOProtocol,
        current_user: UserProtocol,
    ) -> DatasetGetResponse:
        cmd = GetOrCreateDatasetCommand(
            dao=dao,
            data={
                "table_name": data.table_name,
                "database": data.database,
                "schema_name": data.schema_name,
                "template_params": data.template_params,
                "normalize_columns": data.normalize_columns,
                "always_filter_main_dttm": data.always_filter_main_dttm,
            },
            user_id=current_user.id,
        )
        dataset = await cmd.execute()
        event_logger.log(
            "dataset.get_or_create",
            object_ref=f"dataset:{dataset.id}",
            user_id=current_user.id,
        )
        return DatasetGetResponse(
            id=dataset.id,
            result={"table_name": dataset.table_name},
        )

    @get(
        "/export/",
        guards=[require_permission("can_read", "Dataset")],
        media_type="application/zip",
    )
    async def export(
        self,
        dao: DatasetDAOProtocol,
        rison_params: dict[str, Any] | None,
        token: str | None = Parameter(query="token", default=None),
    ) -> Stream:
        ids = extract_ids(rison_params)
        if not ids:
            raise CommandInvalidError("At least one ID is required for export")
        cmd = ExportDatasetsCommand(model_ids=ids, dao=dao)
        buf = await cmd.execute()
        event_logger.log("dataset.export", extra={"count": len(ids)})
        return Stream(
            stream_zip(buf),
            status_code=200,
            media_type="application/zip",
            headers=build_export_headers("datasets_export.zip", token=token),
        )

    @post(
        "/import/",
        guards=[require_permission("can_write", "Dataset")],
        media_type="application/json",
    )
    async def import_dataset(
        self,
        dao: DatasetDAOProtocol,
        data: UploadFile = Body(media_type=RequestEncodingType.MULTI_PART),  # noqa: B008
        overwrite: bool = False,
        passwords: str | None = None,
        ssh_tunnel_passwords: str | None = None,
        sync_columns: bool = True,
        sync_metrics: bool = True,
    ) -> dict[str, str]:
        import json as _json

        contents = await data.read()
        buf = io.BytesIO(contents)
        try:
            passwords_dict: dict[str, str] = _json.loads(passwords) if passwords else {}
        except (ValueError, _json.JSONDecodeError):
            raise CommandInvalidError("Invalid JSON in 'passwords' field")
        try:
            ssh_dict: dict[str, str] = (
                _json.loads(ssh_tunnel_passwords) if ssh_tunnel_passwords else {}
            )
        except (ValueError, _json.JSONDecodeError):
            raise CommandInvalidError("Invalid JSON in 'ssh_tunnel_passwords' field")
        cmd = ImportDatasetsCommand(
            contents=buf,
            dao=dao,
            overwrite=overwrite,
            passwords=passwords_dict,
            ssh_tunnel_passwords=ssh_dict,
            sync_columns=sync_columns,
            sync_metrics=sync_metrics,
        )
        await cmd.execute()
        event_logger.log("dataset.import")
        return {"message": "OK"}

    @put(
        "/warm_up_cache",
        guards=[require_permission("can_write", "Dataset")],
    )
    async def warm_up_cache(
        self, data: DatasetCacheWarmUpRequest, dao: DatasetDAOProtocol
    ) -> dict[str, Any]:
        cmd = WarmUpDatasetCacheCommand(
            dao=dao,
            db_name=data.db_name,
            table_name=data.table_name,
            dashboard_id=data.dashboard_id,
            extra_filters=data.extra_filters,
        )
        result = await cmd.execute()
        event_logger.log("dataset.warm_up_cache")
        return {"result": result}

    @get(
        "/{pk:int}/related_objects",
        guards=[require_permission("can_read", "Dataset")],
    )
    async def related_objects(self, pk: int, dao: DatasetDAOProtocol) -> dict[str, Any]:
        """GET related objects (charts/dashboards using dataset)."""
        dataset = await dao.find_by_id(pk)
        if not dataset:
            raise ObjectNotFoundError("Dataset", pk)
        related = await dao.get_related_objects(pk)
        charts = related.get("charts", [])
        dashboards = related.get("dashboards", [])
        return {
            "charts": {
                "count": len(charts),
                "result": [
                    {
                        "id": c.id,
                        "slice_name": getattr(c, "slice_name", ""),
                        "viz_type": getattr(c, "viz_type", ""),
                    }
                    for c in charts
                ],
            },
            "dashboards": {
                "count": len(dashboards),
                "result": [
                    {
                        "id": d.id,
                        "slug": getattr(d, "slug", None),
                        "title": getattr(d, "dashboard_title", ""),
                        "json_metadata": getattr(d, "json_metadata", None),
                    }
                    for d in dashboards
                ],
            },
        }

    @get(
        "/{pk:int}/drill_info/",
        guards=[require_permission("can_read", "Dataset")],
    )
    async def drill_info(
        self,
        pk: int,
        dao: DatasetDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        q: str | None = Parameter(query="q", default=None),
    ) -> dict[str, Any]:
        """GET /api/v1/dataset/{pk}/drill_info/ — drill-down column info.

        Accepts optional RISON ``q`` parameter with ``dashboard_id`` to
        enable guest-token / RBAC fallback access checks.
        """
        import json as _json

        dashboard_id: int | None = None
        if q:
            try:
                rison_parsed = _json.loads(q)
                dashboard_id = rison_parsed.get("dashboard_id")
            except (ValueError, _json.JSONDecodeError, TypeError):
                pass

        dataset = await dao.find_by_id(pk)
        if not dataset:
            raise ObjectNotFoundError("Dataset", pk)

        # When dashboard_id is provided, verify access via RBAC or
        # guest-token scoped to that dashboard.
        if dashboard_id is not None:
            try:
                await security_manager.raise_for_access(
                    datasource=dataset,
                    dashboard_id=dashboard_id,
                    user=current_user,
                )
            except Exception:  # noqa: BLE001
                # Fallback: allow access if user can read the dataset directly
                pass

        columns = getattr(dataset, "columns", []) or []
        return {
            "columns": [
                {
                    "column_name": getattr(col, "column_name", ""),
                    "groupby": getattr(col, "groupby", True),
                    "is_dttm": getattr(col, "is_dttm", False),
                    "type": getattr(col, "type", ""),
                }
                for col in columns
            ]
        }

    # ------------------------------------------------------------------
    # Column/Metric delete endpoints (merged from DatasetColumnsController
    # and DatasetMetricController)
    # ------------------------------------------------------------------

    @delete(
        "/{pk:int}/column/{column_id:int}",
        guards=[require_permission("can_write", "Dataset")],
        status_code=200,
    )
    async def delete_column(
        self,
        pk: int,
        column_id: int,
        dao: DatasetDAOProtocol,
        column_dao: ColumnDAOProtocol,
    ) -> dict[str, str]:
        cmd = DeleteDatasetColumnCommand(
            dataset_dao=dao,
            column_dao=column_dao,
            dataset_id=pk,
            column_id=column_id,
        )
        await cmd.execute()
        event_logger.log(
            "dataset.delete_column",
            object_ref=f"dataset:{pk}/column:{column_id}",
        )
        return {"message": "OK"}

    @delete(
        "/{pk:int}/metric/{metric_id:int}",
        guards=[require_permission("can_write", "Dataset")],
        status_code=200,
    )
    async def delete_metric(
        self,
        pk: int,
        metric_id: int,
        dao: DatasetDAOProtocol,
        metric_dao: MetricDAOProtocol,
    ) -> dict[str, str]:
        cmd = DeleteDatasetMetricCommand(
            dataset_dao=dao,
            metric_dao=metric_dao,
            dataset_id=pk,
            metric_id=metric_id,
        )
        await cmd.execute()
        event_logger.log(
            "dataset.delete_metric",
            object_ref=f"dataset:{pk}/metric:{metric_id}",
        )
        return {"message": "OK"}
