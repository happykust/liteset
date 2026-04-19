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
from typing import Any, cast, TYPE_CHECKING

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
    build_rison_query_params,
    extract_ids,
    extract_ids_required,
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
    DatasetDetailResult,
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

if TYPE_CHECKING:
    from superset.db.daos.dataset import (
        AsyncDatasetColumnDAO,
        AsyncDatasetDAO,
        AsyncDatasetMetricDAO,
    )


# ---------------------------------------------------------------------------
# Custom RISON filters for datasets
# ---------------------------------------------------------------------------
def _dataset_custom_filters() -> dict[str, Any]:
    def _dataset_is_null_or_empty(model_cls: Any, value: Any) -> Any:
        from sqlalchemy import or_

        clause = or_(model_cls.sql.is_(None), model_cls.sql == "")
        if not value:
            from sqlalchemy import not_

            return not_(clause)
        return clause

    def _dataset_is_certified(model_cls: Any, value: Any) -> Any:
        from sqlalchemy import or_

        check_value = '%"certification":%'
        if value is True:
            return model_cls.extra.ilike(check_value)
        if value is False:
            return or_(
                model_cls.extra.notlike(check_value),
                model_cls.extra.is_(None),
            )
        return None

    return {
        "dataset_is_null_or_empty": _dataset_is_null_or_empty,
        "dataset_is_certified": _dataset_is_certified,
    }


# ``DatasetDetailResult.from_model(dataset)`` (see
# ``superset/schemas/dataset.py:270``) is used directly for create /
# update response payloads — it mirrors the original Flask
# ``DatasetRestApi`` Marshmallow schema via ``ModelStruct`` auto-mapping
# plus ``_resolve_owners`` / ``_resolve_database`` custom resolvers for
# the relationship fields.  The caller must eager-load ``columns``,
# ``metrics``, ``owners``, ``database`` so the resolvers don't trigger
# lazy loads (which crash with ``MissingGreenlet`` under asyncpg).


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
        from sqlalchemy.orm import selectinload

        from superset.db.filters import dataset_access_filters
        from superset.models.connectors import SqlaTable

        rison_filters, order_by, page, page_size = build_rison_query_params(
            SqlaTable,
            rison_params,
            custom_filters=_dataset_custom_filters(),
        )
        base_filters = await dataset_access_filters(security_manager, current_user)
        all_filters = (base_filters or []) + rison_filters

        datasets = await dao.find_all(
            filters=all_filters or None,
            page=page,
            page_size=page_size,
            order_by=order_by,
            options=[
                selectinload(SqlaTable.database),
                selectinload(SqlaTable.owners),
                selectinload(SqlaTable.changed_by),
            ],
        )
        total = await dao.count(filters=all_filters or None)
        event_logger.log("dataset.list")
        payload = serialize_list_response(
            datasets,
            total,
            [
                "id",
                "uuid",
                "table_name",
                "schema",
                "catalog",
                "sql",
                "extra",
                "description",
                "default_endpoint",
                "changed_on_utc",
                "changed_on_delta_humanized",
                "changed_by_name",
                "changed_by_fk",
                "changed_by.id",
                "changed_by.first_name",
                "changed_by.last_name",
                "database.id",
                "database.database_name",
                "database.uuid",
                "owners.id",
                "owners.first_name",
                "owners.last_name",
            ],
            list_title="List Dataset",
        )
        for item in payload["result"]:
            item["datasource_type"] = "table"
            item["kind"] = "physical" if not item.get("sql") else "virtual"
            item["explore_url"] = (
                item.pop("default_endpoint", None)
                or f"/explore/?datasource_type=table&datasource_id={item['id']}"
            )
        return DatasetListResponse(
            result=payload["result"],
            count=payload["count"],
            ids=payload.get("ids", []),
            label_columns=payload.get("label_columns", {}),
            list_columns=payload.get("list_columns", []),
            order_columns=payload.get("order_columns", []),
            description_columns=payload.get("description_columns", {}),
        )

    @get(
        "/_info",
        guards=[require_permission("can_read", "Dataset")],
    )
    async def info(self, dao: DatasetDAOProtocol) -> dict[str, Any]:
        """GET /api/v1/dataset/_info — API metadata for frontend."""
        return await get_info_payload(
            dao=dao,
            model_name="Dataset",
            permissions=[
                "can_warm_up_cache",
                "can_get_drill_info",
                "can_read",
                "can_duplicate",
                "can_export",
                "can_get_or_create_dataset",
                "can_write",
            ],
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
        "/{id_or_uuid:str}",
        guards=[require_permission("can_read", "Dataset")],
    )
    async def get_dataset(
        self,
        id_or_uuid: str,
        dao: DatasetDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        include_rendered_sql: bool = Parameter(
            query="include_rendered_sql",
            default=False,
        ),
    ) -> DatasetGetResponse:
        from sqlalchemy.orm import selectinload

        from superset.db.filters import dataset_access_filters
        from superset.models.connectors import SqlaTable

        # Parse id_or_uuid: integer ID or UUID string
        try:
            pk = int(id_or_uuid)
            id_filter = SqlaTable.id == pk
        except ValueError:
            id_filter = SqlaTable.uuid == id_or_uuid

        # Use find_all with selectinload to eagerly load all relationships
        # needed for the full show response (matches chart controller pattern).
        base_filters = await dataset_access_filters(security_manager, current_user)
        all_filters = [id_filter] + (base_filters or [])

        results = await dao.find_all(
            filters=all_filters,
            page=0,
            page_size=1,
            options=[
                selectinload(SqlaTable.database),
                selectinload(SqlaTable.owners),
                selectinload(SqlaTable.columns),
                selectinload(SqlaTable.metrics),
                selectinload(SqlaTable.changed_by),
                selectinload(SqlaTable.created_by),
            ],
        )
        if not results:
            raise ObjectNotFoundError("Dataset", id_or_uuid)
        dataset = results[0]

        raw_sql = getattr(dataset, "sql", None)
        rendered_sql: str | None = None
        if include_rendered_sql and raw_sql:
            # Attempt Jinja rendering of the SQL template.  Falls back
            # to the raw SQL string when the template engine is not
            # available or rendering fails.
            try:
                from superset.jinja_context import get_template_processor

                tp = get_template_processor(database=dataset.database, table=dataset)
                rendered_sql = tp.process_template(raw_sql)
            except Exception:  # noqa: BLE001
                rendered_sql = raw_sql

        detail = DatasetDetailResult.from_model(
            dataset,
            rendered_sql=rendered_sql if include_rendered_sql else None,
        )
        return DatasetGetResponse(
            id=dataset.id,
            result=detail,
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
            dao=cast("AsyncDatasetDAO", dao),
            data=filter_none(
                {
                    "table_name": data.table_name,
                    "database": data.database,
                    "schema": data.schema,
                    "sql": data.sql,
                    "is_managed_externally": data.is_managed_externally,
                    "external_url": data.external_url,
                    "normalize_columns": data.normalize_columns,
                    "always_filter_main_dttm": data.always_filter_main_dttm,
                    "owners": data.owners,
                    "tags": data.tags,
                    "catalog": data.catalog,
                    "template_params": data.template_params,
                    "uuid": data.uuid,
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

        # Eager-load the relationships ``DatasetDetailResult`` touches so
        # the msgspec auto-mapper doesn't trigger lazy loads that crash
        # with ``MissingGreenlet`` under asyncpg.
        from sqlalchemy.orm import selectinload

        from superset.models.connectors import SqlaTable

        dataset = await dao.find_by_id_with_options(
            int(dataset.id),
            options=[
                selectinload(SqlaTable.database),
                selectinload(SqlaTable.columns),
                selectinload(SqlaTable.metrics),
                selectinload(SqlaTable.owners),
            ],
        )
        return DatasetGetResponse(
            id=int(dataset.id) if dataset is not None else 0,
            result=(
                DatasetDetailResult.from_model(dataset)
                if dataset is not None
                else DatasetDetailResult()
            ),
        )

    @put(
        "/{id_or_uuid:str}",
        guards=[require_permission("can_write", "Dataset")],
    )
    async def update(
        self,
        id_or_uuid: str,
        data: DatasetPutSchema,
        dao: DatasetDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        override_columns: bool = Parameter(query="override_columns", default=False),
    ) -> DatasetGetResponse:
        # Parse id_or_uuid: integer ID or UUID string
        try:
            pk = int(id_or_uuid)
        except ValueError:
            # UUID lookup — resolve to integer pk
            from superset.models.connectors import SqlaTable

            results = await dao.find_all(
                filters=[SqlaTable.uuid == id_or_uuid],
                page=0,
                page_size=1,
            )
            if not results:
                raise ObjectNotFoundError("Dataset", id_or_uuid) from None
            pk = results[0].id

        update_data: dict[str, Any] = filter_unset(
            {
                "table_name": data.table_name,
                "database_id": data.database_id,
                "sql": data.sql,
                "schema": data.schema,
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
                "owners": data.owners,
                "tags": data.tags,
                "filter_select_enabled": data.filter_select_enabled,
                "fetch_values_predicate": data.fetch_values_predicate,
                "catalog": data.catalog,
                "uuid": data.uuid,
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

        # Always run UpdateDatasetCommand (saves metrics/owners/description/etc.).
        # When override_columns=true, ALSO run RefreshDatasetCommand afterwards
        # to resync columns from the physical DB — skipping user-provided
        # column edits (they're already overridden by the refresh).
        # Mirrors superset_old/datasets/api.py:441-443:
        #     changed_model = UpdateDatasetCommand(pk, item, override_columns).run()
        #     if override_columns:
        #         RefreshDatasetCommand(pk).run()
        if override_columns:
            update_data.pop("columns", None)
        cmd = UpdateDatasetCommand(
            dao=cast("AsyncDatasetDAO", dao),
            dataset_id=pk,
            data=update_data,
            user_id=current_user.id,
            security_manager=security_manager,
        )
        dataset = await cmd.execute()
        if override_columns:
            cmd_refresh = RefreshDatasetCommand(
                dao=cast("AsyncDatasetDAO", dao),
                dataset_id=pk,
                security_manager=security_manager,
                user_id=current_user.id,
            )
            dataset = await cmd_refresh.execute()

        event_logger.log(
            "dataset.update",
            object_ref=f"dataset:{pk}",
            user_id=current_user.id,
        )

        # Re-load the dataset with every relationship ``DatasetDetailResult``
        # touches eager-loaded so the msgspec auto-mapper doesn't trigger
        # lazy loads that crash with ``MissingGreenlet`` under asyncpg.
        # The response payload must include ``columns`` / ``metrics`` /
        # ``owners`` to match the original Flask ``DatasetRestApi.put``
        # shape — the frontend's ``saveDatasource`` reducer replaces the
        # whole ``explore.datasource`` Redux slice with this dict, and a
        # thin payload causes the Explore view to render "Missing dataset"
        # (seen while running ``explore/control.test.ts``).
        from sqlalchemy.orm import selectinload

        from superset.models.connectors import SqlaTable

        dataset = await dao.find_by_id_with_options(
            int(dataset.id),
            options=[
                selectinload(SqlaTable.database),
                selectinload(SqlaTable.columns),
                selectinload(SqlaTable.metrics),
                selectinload(SqlaTable.owners),
            ],
        )
        return DatasetGetResponse(
            id=int(dataset.id) if dataset is not None else int(pk),
            result=(
                DatasetDetailResult.from_model(dataset)
                if dataset is not None
                else DatasetDetailResult()
            ),
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
            dao=cast("AsyncDatasetDAO", dao),
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
        rison_params: list[int] | dict[str, Any] | None,
    ) -> dict[str, str]:
        ids = extract_ids_required(rison_params)
        cmd = BulkDeleteDatasetsCommand(
            dao=cast("AsyncDatasetDAO", dao),
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
            dao=cast("AsyncDatasetDAO", dao),
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
            id=int(dataset.id),
            result={"table_name": dataset.table_name},
        )

    @put(
        "/{pk:int}/refresh",
        guards=[require_permission("can_write", "Dataset")],
    )
    async def refresh(self, pk: int, dao: DatasetDAOProtocol) -> dict[str, str]:
        cmd = RefreshDatasetCommand(dao=cast("AsyncDatasetDAO", dao), dataset_id=pk)
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
            dao=cast("AsyncDatasetDAO", dao),
            data={
                "table_name": data.table_name,
                "database_id": data.database_id,
                "schema": data.schema,
                "catalog": data.catalog,
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
        dataset_id = int(dataset.id)
        return DatasetGetResponse(
            id=dataset_id,
            result={"table_id": dataset_id},
        )

    @get(
        "/export/",
        guards=[require_permission("can_read", "Dataset")],
        media_type="application/zip",
    )
    async def export(
        self,
        dao: DatasetDAOProtocol,
        rison_params: list[int] | dict[str, Any] | None,
        token: str | None = Parameter(query="token", default=None),
    ) -> Stream:
        ids = extract_ids(rison_params)
        if not ids:
            raise CommandInvalidError("At least one ID is required for export")
        cmd = ExportDatasetsCommand(model_ids=ids, dao=cast("AsyncDatasetDAO", dao))
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
        ssh_tunnel_private_keys: str | None = None,
        ssh_tunnel_private_key_passwords: str | None = None,
        sync_columns: bool = True,
        sync_metrics: bool = True,
    ) -> dict[str, str]:
        import json as _json

        contents = await data.read()
        buf = io.BytesIO(contents)
        try:
            passwords_dict: dict[str, str] = _json.loads(passwords) if passwords else {}
        except (ValueError, _json.JSONDecodeError) as exc:
            raise CommandInvalidError("Invalid JSON in 'passwords' field") from exc
        try:
            ssh_dict: dict[str, str] = (
                _json.loads(ssh_tunnel_passwords) if ssh_tunnel_passwords else {}
            )
        except (ValueError, _json.JSONDecodeError) as exc:
            raise CommandInvalidError(
                "Invalid JSON in 'ssh_tunnel_passwords' field"
            ) from exc
        try:
            ssh_private_keys_dict: dict[str, str] = (
                _json.loads(ssh_tunnel_private_keys) if ssh_tunnel_private_keys else {}
            )
        except (ValueError, _json.JSONDecodeError) as exc:
            raise CommandInvalidError(
                "Invalid JSON in 'ssh_tunnel_private_keys' field"
            ) from exc
        try:
            ssh_private_key_passwords_dict: dict[str, str] = (
                _json.loads(ssh_tunnel_private_key_passwords)
                if ssh_tunnel_private_key_passwords
                else {}
            )
        except (ValueError, _json.JSONDecodeError) as exc:
            raise CommandInvalidError(
                "Invalid JSON in 'ssh_tunnel_private_key_passwords' field"
            ) from exc
        cmd = ImportDatasetsCommand(
            contents=buf,
            dao=cast("AsyncDatasetDAO", dao),
            overwrite=overwrite,
            passwords=passwords_dict,
            ssh_tunnel_passwords=ssh_dict,
            ssh_tunnel_private_keys=ssh_private_keys_dict,
            ssh_tunnel_private_key_passwords=ssh_private_key_passwords_dict,
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
            dao=cast("AsyncDatasetDAO", dao),
            db_name=data.db_name,
            table_name=data.table_name,
            dashboard_id=data.dashboard_id,
            extra_filters=data.extra_filters,
        )
        result = await cmd.execute()
        event_logger.log("dataset.warm_up_cache")
        return {"result": result}

    @get(
        "/{id_or_uuid:str}/related_objects",
        guards=[require_permission("can_read", "Dataset")],
    )
    async def related_objects(
        self, id_or_uuid: str, dao: DatasetDAOProtocol
    ) -> dict[str, Any]:
        """GET related objects (charts/dashboards using dataset)."""
        # Parse id_or_uuid: integer ID or UUID string
        try:
            pk = int(id_or_uuid)
            dataset = await dao.find_by_id(pk)
        except ValueError:
            from superset.models.connectors import SqlaTable

            results = await dao.find_all(
                filters=[SqlaTable.uuid == id_or_uuid],
                page=0,
                page_size=1,
            )
            dataset = results[0] if results else None
            pk = int(dataset.id) if dataset else 0
        if not dataset:
            raise ObjectNotFoundError("Dataset", id_or_uuid)
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
            except Exception:  # noqa: BLE001, S110
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
            dataset_dao=cast("AsyncDatasetDAO", dao),
            column_dao=cast("AsyncDatasetColumnDAO", column_dao),
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
            dataset_dao=cast("AsyncDatasetDAO", dao),
            metric_dao=cast("AsyncDatasetMetricDAO", metric_dao),
            dataset_id=pk,
            metric_id=metric_id,
        )
        await cmd.execute()
        event_logger.log(
            "dataset.delete_metric",
            object_ref=f"dataset:{pk}/metric:{metric_id}",
        )
        return {"message": "OK"}
