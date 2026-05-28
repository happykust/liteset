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

from superset.commands.dataset.columns.delete import DeleteDatasetColumnCommand
from superset.commands.dataset.create import (
    CreateDatasetCommand,
    GetOrCreateDatasetCommand,
)
from superset.commands.dataset.delete import (
    BulkDeleteDatasetsCommand,
    DeleteDatasetCommand,
)
from superset.commands.dataset.duplicate import DuplicateDatasetCommand
from superset.commands.dataset.export import ExportDatasetsCommand
from superset.commands.dataset.metrics.delete import DeleteDatasetMetricCommand
from superset.commands.dataset.refresh import RefreshDatasetCommand
from superset.commands.dataset.update import UpdateDatasetCommand
from superset.commands.dataset.warm_up_cache import WarmUpDatasetCacheCommand
from superset.commands.importers.exceptions import NoValidFilesFoundError

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
from superset.importexport.legacy.dispatcher import (
    ImportDatasetsCommand as LegacyImportDatasetsDispatcher,
)
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


def _parse_import_upload(filename: str, contents: bytes) -> tuple[dict[str, str], bool]:
    """Split an uploaded import payload into ``({filename: text}, is_zip)``.

    Mirrors ``superset_old/datasets/api.py:919-927``: ZIP bundles are decoded
    with ``get_contents_from_bundle`` (``remove_root`` + YAML-only filtering),
    while a non-ZIP upload is treated as a single legacy (v0) JSON document
    keyed by its filename. Empty contents raise
    :class:`NoValidFilesFoundError`, matching the original.
    """
    import zipfile

    from superset.commands.importers.v1.utils import get_contents_from_bundle

    buf = io.BytesIO(contents)
    if zipfile.is_zipfile(buf):
        buf.seek(0)
        with zipfile.ZipFile(buf) as bundle:
            parsed = get_contents_from_bundle(bundle)
        is_zip = True
    else:
        parsed = {filename: contents.decode("utf-8")}
        is_zip = False

    if not parsed:
        raise NoValidFilesFoundError()
    return parsed, is_zip


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
        await event_logger.alog_with_context("dataset.list")
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
        security_manager: SecurityManagerProtocol,
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
            security_manager=security_manager,
        )
        dataset = await cmd.execute()
        await event_logger.alog_with_context(
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
                selectinload(SqlaTable.created_by),
                selectinload(SqlaTable.changed_by),
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

        await event_logger.alog_with_context(
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
                selectinload(SqlaTable.created_by),
                selectinload(SqlaTable.changed_by),
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
        await event_logger.alog_with_context(
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
        await event_logger.alog_with_context(
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
        await event_logger.alog_with_context(
            "dataset.duplicate",
            object_ref=f"dataset:{dataset.id}",
            user_id=current_user.id,
        )
        # Mirrors ``superset_old/datasets/api.py:635``:
        # ``self.response(201, id=new_model.id, result=item)`` where ``item``
        # is the loaded ``DatasetDuplicateSchema`` (``base_model_id`` +
        # ``table_name``).
        return DatasetGetResponse(
            id=int(dataset.id),
            result={
                "base_model_id": data.base_model_id,
                "table_name": data.table_name,
            },
        )

    @put(
        "/{pk:int}/refresh",
        guards=[require_permission("can_write", "Dataset")],
    )
    async def refresh(
        self,
        pk: int,
        dao: DatasetDAOProtocol,
        current_user: UserProtocol,
        security_manager: SecurityManagerProtocol,
    ) -> dict[str, str]:
        cmd = RefreshDatasetCommand(
            dao=cast("AsyncDatasetDAO", dao),
            dataset_id=pk,
            security_manager=security_manager,
            user_id=current_user.id,
        )
        await cmd.execute()
        await event_logger.alog_with_context(
            "dataset.refresh", object_ref=f"dataset:{pk}"
        )
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
        await event_logger.alog_with_context(
            "dataset.get_or_create",
            object_ref=f"dataset:{dataset.id}",
            user_id=current_user.id,
        )
        dataset_id = int(dataset.id)
        # Mirrors ``superset_old/datasets/api.py:1016/1021``:
        # ``self.response(200, result={"table_id": table.id})`` — no top-level
        # ``id``. ``ApiResponse`` has ``omit_defaults=True`` so leaving ``id``
        # unset omits it from the payload.
        return DatasetGetResponse(
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
        # 1:1 with ``superset_old/datasets/api.py:553-579``: build
        # ``root = f"dataset_export_{timestamp}"`` (timestamp =
        # ``datetime.now().strftime("%Y%m%dT%H%M%S")``), nest every ZIP entry
        # under ``f"{root}/{file_name}"``, and name the download
        # ``f"{root}.zip"``. The importer strips the root back off via
        # ``remove_root`` (``parts[1:]``) so re-import still works.
        from datetime import datetime as _datetime

        timestamp = _datetime.now().strftime("%Y%m%dT%H%M%S")
        root = f"dataset_export_{timestamp}"
        cmd = ExportDatasetsCommand(model_ids=ids, dao=cast("AsyncDatasetDAO", dao))
        cmd._root = root  # noqa: SLF001
        buf = await cmd.execute()
        await event_logger.alog_with_context(
            "dataset.export", extra={"count": len(ids)}
        )
        return Stream(
            stream_zip(buf),
            status_code=200,
            media_type="application/zip",
            headers=build_export_headers(f"{root}.zip", token=token),
        )

    @post(
        "/import/",
        guards=[require_permission("can_write", "Dataset")],
        media_type="application/json",
        # Upstream returns 200 "OK" (datasets/api.py import_); align.
        status_code=200,
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

        # Mirror ``superset_old/datasets/api.py:919-963``: a ZIP bundle is
        # parsed (remove_root + YAML filter) and dispatched v1-then-v0; a
        # non-ZIP upload is a single legacy (v0) JSON document. The dispatcher
        # (``superset/importexport/legacy/dispatcher.py``) tries the async v1
        # command first and falls back to the sync v0 command on
        # ``IncorrectVersionError`` — matching the original
        # ``commands/dataset/importers/dispatcher.py``.
        filename = data.filename or "import.json"
        parsed, is_zip = _parse_import_upload(filename, contents)
        if is_zip:
            dispatcher = LegacyImportDatasetsDispatcher(
                parsed,
                overwrite=overwrite,
                passwords=passwords_dict,
                ssh_tunnel_passwords=ssh_dict,
                ssh_tunnel_private_keys=ssh_private_keys_dict,
                ssh_tunnel_private_key_passwords=ssh_private_key_passwords_dict,
                sync_columns=sync_columns,
                sync_metrics=sync_metrics,
            )
            await dispatcher.run_async(dao=cast("AsyncDatasetDAO", dao))
        else:
            # A single JSON document is unversioned (v0). The modern v1
            # importer always requires a ZIP with ``metadata.yaml``, so route
            # straight to the sync v0 legacy command (run in a worker thread
            # because it uses a sync ``Session``), matching the v0 fallback the
            # original dispatcher reaches in this path.
            import asyncio as _asyncio

            from superset.importexport.legacy.dataset_v0 import (
                ImportDatasetsCommand as V0ImportDatasetsCommand,
            )

            await _asyncio.to_thread(
                V0ImportDatasetsCommand(
                    parsed,
                    sync_columns=sync_columns,
                    sync_metrics=sync_metrics,
                ).run,
            )
        await event_logger.alog_with_context("dataset.import")
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
        await event_logger.alog_with_context("dataset.warm_up_cache")
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

        Direct port of ``superset_old/datasets/api.py:1191-1287`` +
        ``DatasetDrillInfoSchema`` (``schemas.py:399-441``).

        The RISON ``q`` parameter carries an optional ``dashboard_id`` that
        enables the embedded (guest) / DASHBOARD_RBAC drill-through fallback.
        The response shape is ``{"result": DatasetDrillInfoSchema}`` —
        ``columns`` are filtered to ``groupby=True`` dimensions, and a guest
        user only ever sees ``{"id", "columns"}``.
        """
        from sqlalchemy.orm import selectinload

        from superset.db.filters import dataset_access_filters
        from superset.exceptions import ForbiddenError
        from superset.models.connectors import SqlaTable

        dashboard_id: int | None = self._parse_drill_dashboard_id(q)

        # Eager-load relationships used by ``DatasetDrillInfoSchema`` so they
        # don't trigger lazy loads (MissingGreenlet under asyncpg).
        drill_options = [
            selectinload(SqlaTable.columns),
            selectinload(SqlaTable.owners),
            selectinload(SqlaTable.created_by),
            selectinload(SqlaTable.changed_by),
        ]

        # First try with regular access (apply the dataset access base
        # filter, mirroring ``self.datamodel.get(pk, self._base_filters, ...)``).
        base_filters = await dataset_access_filters(security_manager, current_user)
        results = await dao.find_all(
            filters=[SqlaTable.id == pk] + (base_filters or []),
            page=0,
            page_size=1,
            options=drill_options,
        )
        if results:
            return {"result": self._dump_drill_info(results[0], security_manager,
                                                     current_user)}

        # Embedded (guest) user must pass a dashboard id.
        if not dashboard_id and security_manager.is_guest_user(current_user):
            raise ForbiddenError()
        # RBAC user must pass a dashboard id for fallback validation.
        if not dashboard_id:
            raise ObjectNotFoundError("Dataset", pk)

        # Lazy-load the dashboard and dataset (skipping base filters) for the
        # RBAC / embedded drill-through access check.
        dashboard_dao = self._dashboard_dao(dao)
        dashboard = await dashboard_dao.find_by_id_with_options(
            int(dashboard_id),
            options=[
                selectinload(dashboard_dao.model_cls.roles),
                selectinload(dashboard_dao.model_cls.slices),
            ],
        )
        dataset_ = await dao.find_by_id_with_options(
            pk, options=[selectinload(SqlaTable.slices)]
        )
        if not (dashboard and dataset_):
            raise ObjectNotFoundError("Dataset", pk)
        if not await self._can_drill_dataset_via_dashboard_access(
            dataset_, dashboard, security_manager, current_user
        ):
            raise ForbiddenError()

        # Reload the dataset skipping base filters with the eager loads needed
        # by the schema dump (we avoid reusing ``dataset_`` so column lazy
        # loads don't fire).
        dataset = await dao.find_by_id_with_options(pk, options=drill_options)
        if not dataset:
            raise ObjectNotFoundError("Dataset", pk)
        return {"result": self._dump_drill_info(dataset, security_manager,
                                                current_user)}

    @staticmethod
    def _parse_drill_dashboard_id(q: str | None) -> int | None:
        """Extract ``dashboard_id`` from the RISON (or JSON) ``q`` parameter."""
        if not q:
            return None
        import json as _json

        import prison

        parsed: Any = None
        try:
            parsed = prison.loads(q)
        except (ValueError, TypeError):
            try:
                parsed = _json.loads(q)
            except (ValueError, _json.JSONDecodeError, TypeError):
                parsed = None
        if isinstance(parsed, dict):
            return parsed.get("dashboard_id")
        return None

    @staticmethod
    def _dashboard_dao(dataset_dao: DatasetDAOProtocol) -> Any:
        """Build an ``AsyncDashboardDAO`` bound to the dataset DAO's session."""
        from superset.db.daos.dashboard import AsyncDashboardDAO

        return AsyncDashboardDAO(dataset_dao.session)  # type: ignore[attr-defined]

    @staticmethod
    def _dump_drill_info(
        dataset: Any,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """Serialize a dataset into the ``DatasetDrillInfoSchema`` shape.

        Port of ``DatasetDrillInfoSchema`` (``superset_old/datasets/schemas.py``
        :399-441): ``columns`` are filtered to ``groupby=True`` dimensions and
        carry ``{column_name, verbose_name}``; a guest user only ever receives
        ``{"id", "columns"}``.
        """

        def _user(u: Any) -> dict[str, Any] | None:
            if u is None:
                return None
            return {
                "first_name": getattr(u, "first_name", None),
                "last_name": getattr(u, "last_name", None),
            }

        columns = [
            {
                "column_name": getattr(col, "column_name", ""),
                "verbose_name": getattr(col, "verbose_name", None),
            }
            for col in (getattr(dataset, "columns", []) or [])
            if getattr(col, "groupby", False)
        ]

        if security_manager.is_guest_user(current_user):
            return {"id": dataset.id, "columns": columns}

        return {
            "id": dataset.id,
            "table_name": getattr(dataset, "table_name", None),
            "columns": columns,
            "owners": [_user(o) for o in (getattr(dataset, "owners", []) or [])],
            "created_by": _user(getattr(dataset, "created_by", None)),
            "created_on_humanized": getattr(
                dataset, "created_on_delta_humanized", None
            ),
            "changed_by": _user(getattr(dataset, "changed_by", None)),
            "changed_on_humanized": getattr(
                dataset, "changed_on_delta_humanized", None
            ),
        }

    @staticmethod
    async def _can_drill_dataset_via_dashboard_access(
        dataset: Any,
        dashboard: Any,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> bool:
        """Async port of
        ``SupersetSecurityManager.can_drill_dataset_via_dashboard_access``
        (``superset_old/security/manager.py:576-600``).

        True when an embedded guest with guest-access to the dashboard, or a
        DASHBOARD_RBAC user whose roles intersect a published dashboard's
        roles, drills a dataset that the dashboard actually uses.
        """
        from superset.utils.feature_flags import feature_flag_manager

        # Datasets used by the dashboard (derived from its table-backed
        # slices — the new model has no ``datasources`` property).
        dashboard_dataset_ids = {
            getattr(slc, "datasource_id", None)
            for slc in (getattr(dashboard, "slices", []) or [])
            if getattr(slc, "datasource_type", None) == "table"
        }
        if dataset.id not in dashboard_dataset_ids:
            return False

        embedded_branch = (
            feature_flag_manager.is_feature_enabled("EMBEDDED_SUPERSET")
            and security_manager.is_guest_user(current_user)
            and await security_manager.has_guest_access(dashboard, user=current_user)
        )

        roles = getattr(dashboard, "roles", []) or []
        user_roles = await security_manager.get_user_roles(current_user)
        rbac_branch = bool(
            feature_flag_manager.is_feature_enabled("DASHBOARD_RBAC")
            and roles
            and getattr(dashboard, "published", False)
            and {role.id for role in roles} & {role.id for role in user_roles}
        )

        return bool(embedded_branch or rbac_branch)

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
        current_user: UserProtocol,
        security_manager: SecurityManagerProtocol,
    ) -> dict[str, str]:
        cmd = DeleteDatasetColumnCommand(
            dataset_dao=cast("AsyncDatasetDAO", dao),
            column_dao=cast("AsyncDatasetColumnDAO", column_dao),
            dataset_id=pk,
            column_id=column_id,
            security_manager=security_manager,
            user_id=current_user.id,
        )
        await cmd.execute()
        await event_logger.alog_with_context(
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
        current_user: UserProtocol,
        security_manager: SecurityManagerProtocol,
    ) -> dict[str, str]:
        cmd = DeleteDatasetMetricCommand(
            dataset_dao=cast("AsyncDatasetDAO", dao),
            metric_dao=cast("AsyncDatasetMetricDAO", metric_dao),
            dataset_id=pk,
            metric_id=metric_id,
            security_manager=security_manager,
            user_id=current_user.id,
        )
        await cmd.execute()
        await event_logger.alog_with_context(
            "dataset.delete_metric",
            object_ref=f"dataset:{pk}/metric:{metric_id}",
        )
        return {"message": "OK"}
