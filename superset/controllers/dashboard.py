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
"""Dashboard controller — 22 endpoints for dashboard CRUD, export/import, favorites,
embedded, screenshots, and related objects."""

from __future__ import annotations

import io
import logging
from typing import Any, cast, TYPE_CHECKING

from litestar import Controller, delete, get, post, put
from litestar.connection import Request
from litestar.datastructures import State
from litestar.di import Provide
from litestar.params import Parameter
from litestar.response import Response, Stream

from superset.commands.dashboard.copy import CopyDashboardCommand
from superset.commands.dashboard.create import CreateDashboardCommand
from superset.commands.dashboard.delete import (
    BulkDeleteDashboardsCommand,
    DeleteDashboardCommand,
    DeleteEmbeddedDashboardCommand,
)
from superset.commands.dashboard.embedded.upsert import UpsertEmbeddedDashboardCommand
from superset.commands.dashboard.export import ExportDashboardsCommand
from superset.commands.dashboard.fave import AddFavoriteDashboardCommand
from superset.commands.dashboard.permalink.create import (
    CreateDashboardPermalinkCommand,
)
from superset.commands.dashboard.permalink.get import GetDashboardPermalinkCommand
from superset.commands.dashboard.unfave import RemoveFavoriteDashboardCommand
from superset.commands.dashboard.update import (
    UpdateDashboardColorsCommand,
    UpdateDashboardCommand,
    UpdateDashboardFiltersCommand,
)
from superset.commands.importers.exceptions import NoValidFilesFoundError

# DAO imports moved to provider functions (avoid Flask import chain)
from superset.controllers.base import (
    build_rison_query_params,
    extract_ids,
    extract_ids_required,
    get_distinct_payload,
    get_info_payload,
    get_related_payload,
    parse_import_request,
    serialize_list_response,
    stream_zip,
)
from superset.events import event_logger
from superset.exceptions import CommandInvalidError, ObjectNotFoundError
from superset.guards.rbac import (
    deny_anon_with_404,
    require_permission,
)
from superset.importexport.legacy.dispatcher import (
    ImportDashboardsCommand as LegacyImportDashboardsDispatcher,
)
from superset.params.rison import provide_rison_query
from superset.providers import (
    provide_dashboard_dao,
    provide_embedded_dao,
    provide_kv_dao,
)
from superset.schemas.base import FavoriteStatusItem, FavoriteStatusResponse
from superset.schemas.dashboard import (
    DashboardColorsUpdateSchema,
    DashboardCopySchema,
    DashboardDetailResult,
    DashboardFiltersUpdateSchema,
    DashboardGetResponse,
    DashboardPermalinkSchema,
    DashboardPostSchema,
    DashboardPutSchema,
    DashboardScreenshotSchema,
    EmbeddedDashboardConfig,
    EmbeddedDashboardResponse,
)
from superset.typing import (
    DashboardDAOProtocol,
    EmbeddedDAOProtocol,
    KeyValueDAOProtocol,
    SecurityManagerProtocol,
    UserProtocol,
)
from superset.utils import filter_none, filter_unset

if TYPE_CHECKING:
    from superset.db.daos.dashboard import AsyncDashboardDAO, AsyncEmbeddedDashboardDAO
    from superset.db.daos.key_value import AsyncKeyValueDAO

logger = logging.getLogger(__name__)


def _parse_import_upload(filename: str, contents: bytes) -> tuple[dict[str, str], bool]:
    """Split an uploaded import payload into ``({filename: text}, is_zip)``.

    Mirrors ``superset_old/dashboards/api.py:1587-1595``: ZIP bundles are
    decoded with ``get_contents_from_bundle`` (``remove_root`` + YAML-only
    filtering), while a non-ZIP upload is treated as a single legacy (v0)
    JSON document keyed by its filename. Empty contents raise
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


# ---------------------------------------------------------------------------
# Custom RISON filters for dashboards
# ---------------------------------------------------------------------------
def _dashboard_custom_filters(current_user: Any) -> dict[str, Any]:  # noqa: C901
    def _dashboard_is_favorite(model_cls: Any, value: Any) -> Any:
        from sqlalchemy import select as sa_select

        from superset.models.core import FavStar

        user_id = getattr(current_user, "id", None)
        if user_id is None:
            return None
        fav_subq = sa_select(FavStar.obj_id).where(
            FavStar.class_name == "Dashboard",
            FavStar.user_id == user_id,
        )
        if value:
            return model_cls.id.in_(fav_subq)
        return ~model_cls.id.in_(fav_subq)

    def _dashboard_is_certified(model_cls: Any, value: Any) -> Any:
        if value:
            return model_cls.certified_by.isnot(None)
        return model_cls.certified_by.is_(None)

    # ------------------------------------------------------------------
    # Ports of the dashboard list filters from
    # ``superset_old/dashboards/filters.py``.
    # ------------------------------------------------------------------
    def _title_or_slug(model_cls: Any, value: Any) -> Any:
        """``DashboardTitleOrSlugFilter`` (arg ``title_or_slug``)."""
        from sqlalchemy import or_

        if not value:
            return None
        ilike_value = f"%{value}%"
        return or_(
            model_cls.dashboard_title.ilike(ilike_value),
            model_cls.slug.ilike(ilike_value),
        )

    def _dashboard_created_by_me(model_cls: Any, value: Any) -> Any:
        """``DashboardCreatedByMeFilter`` (arg ``dashboard_created_by_me``).

        ``value`` is unused by the original — the filter unconditionally
        narrows to "created or changed by me".
        """
        from sqlalchemy import or_

        del value  # unused to match original semantics
        user_id = getattr(current_user, "id", None)
        if user_id is None:
            return None
        return or_(
            model_cls.created_by_fk == user_id,
            model_cls.changed_by_fk == user_id,
        )

    def _dashboard_has_created_by(model_cls: Any, value: Any) -> Any:
        """``DashboardHasCreatedByFilter`` (arg ``dashboard_has_created_by``)."""
        if value is True:
            return model_cls.created_by_fk.isnot(None)
        if value is False:
            return model_cls.created_by_fk.is_(None)
        return None

    def _dashboard_tags(model_cls: Any, value: Any) -> Any:
        """``DashboardTagNameFilter`` (arg ``dashboard_tags``).

        Filters dashboards associated with a tag by *name*.
        """
        if not value:
            return None
        from sqlalchemy import select as sa_select

        from superset.models.tags import Tag, TaggedObject

        tag_subq = sa_select(TaggedObject.object_id).where(
            TaggedObject.object_type == "dashboard",
            TaggedObject.tag_id == Tag.id,
            Tag.name == value,
        )
        return model_cls.id.in_(tag_subq)

    def _dashboard_tag_id(model_cls: Any, value: Any) -> Any:
        """``DashboardTagIdFilter`` (arg ``dashboard_tag_id``).

        Filters dashboards associated with a tag by *id*.
        """
        if value in (None, ""):
            return None
        try:
            tag_id = int(value)
        except (TypeError, ValueError):
            return None
        from sqlalchemy import select as sa_select

        from superset.models.tags import TaggedObject

        tag_subq = sa_select(TaggedObject.object_id).where(
            TaggedObject.object_type == "dashboard",
            TaggedObject.tag_id == tag_id,
        )
        return model_cls.id.in_(tag_subq)

    return {
        "dashboard_is_favorite": _dashboard_is_favorite,
        "dashboard_is_certified": _dashboard_is_certified,
        "title_or_slug": _title_or_slug,
        "dashboard_created_by_me": _dashboard_created_by_me,
        "dashboard_has_created_by": _dashboard_has_created_by,
        "dashboard_tags": _dashboard_tags,
        "dashboard_tag_id": _dashboard_tag_id,
    }


def _get_time_grain_sqla(database: Any) -> list[list[Any]]:
    """Return ``[(duration, label), ...]`` for the time-grain control.

    1:1 with ``superset_old/connectors/sqla/models.py::SqlaTable.time_grain_sqla``
    which calls ``self.database.grains()`` → ``db_engine_spec.get_time_grains()``.
    The frontend's dashboard filter expects this format to hydrate the
    granularity SelectControl correctly.
    """
    if database is None:
        return []
    try:
        grains = database.grains()
    except Exception:  # noqa: BLE001
        return []
    # ``[(g.duration, g.name), ...]`` — g.name is the human-readable label
    # ("Minute", "5 minute", …); the previous version reused the ISO duration
    # as the label, showing raw "PT5M" codes in the filter dropdown.
    return [[g.duration, g.name] for g in grains]


class DashboardController(Controller):
    path = "/api/v1/dashboard"
    tags = ["Dashboards"]
    dependencies = {
        "dao": Provide(provide_dashboard_dao, sync_to_thread=False),
        "embedded_dao": Provide(provide_embedded_dao, sync_to_thread=False),
        "kv_dao": Provide(provide_kv_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    # ------------------------------------------------------------------
    # GET — list dashboards
    # ------------------------------------------------------------------
    @get(
        "/",
        guards=[require_permission("can_read", "Dashboard")],
    )
    async def get_list(
        self,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/dashboard/ — list dashboards with filtering/pagination."""
        from sqlalchemy.orm import selectinload

        from superset.db.filters import dashboard_access_filters
        from superset.models.dashboard import Dashboard

        rison_filters, order_by, page, page_size = build_rison_query_params(
            Dashboard,
            rison_params,
            custom_filters=_dashboard_custom_filters(current_user),
        )
        # Default ordering: changed_on desc (matches original base_order)
        if order_by is None:
            order_by = [Dashboard.changed_on.desc()]

        base_filters = await dashboard_access_filters(security_manager, current_user)
        all_filters = (base_filters or []) + rison_filters

        dashboards = await dao.find_all(
            filters=all_filters or None,
            page=page,
            page_size=page_size,
            order_by=order_by,
            options=[
                selectinload(Dashboard.owners),
                selectinload(Dashboard.roles),
                selectinload(Dashboard.tags),
                selectinload(Dashboard.changed_by),
                selectinload(Dashboard.created_by),
            ],
        )
        total = await dao.count(filters=all_filters or None)
        await event_logger.alog_with_context("dashboard.list")
        return serialize_list_response(
            dashboards,
            total,
            [
                "id",
                "uuid",
                "published",
                "status",
                "slug",
                "url",
                "dashboard_title",
                "thumbnail_url",
                "certified_by",
                "certification_details",
                "changed_by_name",
                "changed_by.first_name",
                "changed_by.last_name",
                "changed_by.id",
                "changed_on_utc",
                "changed_on_delta_humanized",
                "created_on_delta_humanized",
                "created_by.first_name",
                "created_by.id",
                "created_by.last_name",
                "owners.id",
                "owners.first_name",
                "owners.last_name",
                "roles.id",
                "roles.name",
                "tags.id",
                "tags.name",
                "tags.type",
                "is_managed_externally",
            ],
            list_title="List Dashboard",
            order_columns=[
                "changed_by.first_name",
                "changed_on_delta_humanized",
                "created_by.first_name",
                "dashboard_title",
                "published",
                "changed_on",
            ],
        )

    # ------------------------------------------------------------------
    # GET — API metadata
    # ------------------------------------------------------------------
    @get(
        "/_info",
        guards=[require_permission("can_read", "Dashboard")],
    )
    async def info(
        self,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """GET /api/v1/dashboard/_info — API metadata for frontend."""
        return await get_info_payload(
            dao=dao,
            model_name="Dashboard",
            # Mirrors the original FAB-generated permission list exposed
            # by ``/api/v1/dashboard/_info``; the list order is preserved to
            # match what Cypress snapshots assume.
            permissions=[
                "can_read",
                "can_get_embedded",
                "can_delete_embedded",
                "can_export",
                "can_cache_dashboard_screenshot",
                "can_set_embedded",
                "can_write",
            ],
            security_manager=security_manager,
            current_user=current_user,
            class_permission_name="Dashboard",
        )

    # ------------------------------------------------------------------
    # GET — related values for dropdowns
    # ------------------------------------------------------------------
    @get(
        "/related/{column_name:str}",
        guards=[require_permission("can_read", "Dashboard")],
    )
    async def related(
        self,
        column_name: str,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/dashboard/related/{column_name}"""
        from superset.db.filters import dashboard_access_filters

        base_filters = await dashboard_access_filters(security_manager, current_user)
        return await get_related_payload(
            dao=dao,
            column_name=column_name,
            rison_params=rison_params,
            allowed_fields=frozenset({"owners", "roles", "created_by", "changed_by"}),
            base_filters=base_filters or None,
        )

    # ------------------------------------------------------------------
    # GET — distinct values for filters
    # ------------------------------------------------------------------
    @get(
        "/distinct/{column_name:str}",
        guards=[require_permission("can_read", "Dashboard")],
    )
    async def distinct(
        self,
        column_name: str,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/dashboard/distinct/{column_name}.

        Upstream ``DashboardRestApi`` does not override
        ``allowed_distinct_fields`` → inherits the empty default → every
        distinct request 404s. Mirror that with ``allowed_fields=frozenset()``
        rather than answering with stale or broken (relationship column →
        ``NotImplementedError``) data.
        """
        from superset.db.filters import dashboard_access_filters

        base_filters = await dashboard_access_filters(security_manager, current_user)
        return await get_distinct_payload(
            dao=dao,
            column_name=column_name,
            rison_params=rison_params,
            allowed_fields=frozenset(),
            base_filters=base_filters or None,
        )

    # ------------------------------------------------------------------
    # GET — single dashboard
    # ------------------------------------------------------------------
    @get(
        "/{id_or_slug:str}",
        guards=[require_permission("can_read", "Dashboard")],
    )
    async def get_dashboard(
        self,
        id_or_slug: str,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> DashboardGetResponse:
        from superset.db.filters import dashboard_access_filters

        access_filters = await dashboard_access_filters(security_manager, current_user)
        dashboard = await dao.get_full_by_id_or_slug(
            id_or_slug,
            extra_filters=access_filters,
        )
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", id_or_slug)

        # 1:1 with superset_old/daos/dashboard.py:71-75 — after the access
        # filter, the original performs a secondary ``dashboard.raise_for_access()``
        # which maps a ``SupersetSecurityException`` to ``DashboardAccessDeniedError``
        # → HTTP 403 (vs. the 404 returned by the filter miss above). The async
        # ``raise_for_access`` raises ``SupersetSecurityException`` (403) directly.
        await security_manager.raise_for_access(dashboard=dashboard, user=current_user)

        await event_logger.alog_with_context(
            "dashboard.get", object_ref=f"dashboard:{id_or_slug}"
        )

        result = DashboardDetailResult.from_model(dashboard)

        # Scrub owner and editor identity for guest (embedded Superset) users.
        # 1:1 with superset_old/dashboards/schemas.py::DashboardGetResponseSchema
        # @post_dump hook (lines 244-249): strip ``owners``, ``changed_by``, and
        # ``changed_by_name`` from the response when the current user is a guest.
        is_guest = security_manager.is_guest_user(current_user)
        if is_guest:
            result.owners = []
            result.changed_by = None
            result.changed_by_name = None

        return DashboardGetResponse(
            id=dashboard.id,
            result=result,
        )

    # ------------------------------------------------------------------
    # GET — related datasets
    # ------------------------------------------------------------------
    @get(
        "/{id_or_slug:str}/datasets",
        guards=[require_permission("can_read", "Dashboard")],
    )
    async def get_datasets(  # noqa: C901  # complex business logic
        self,
        id_or_slug: str,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        # Access-scope exactly like the full GET (``get_dashboard``): the
        # ``dashboard_access_filters`` base-filter yields None → 404 for a
        # dashboard the user can't see, then ``raise_for_access`` is the
        # secondary 403 gate. The full loader also lets ``raise_for_access``
        # read owners/roles without a sync lazy-load (MissingGreenlet).
        # Without this, any user with the coarse ``can_read Dashboard`` perm
        # could enumerate the datasets of an inaccessible dashboard.
        from superset.db.filters import dashboard_access_filters

        access_filters = await dashboard_access_filters(security_manager, current_user)
        dashboard = await dao.get_full_by_id_or_slug(
            id_or_slug, extra_filters=access_filters
        )
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", id_or_slug)
        await security_manager.raise_for_access(dashboard=dashboard, user=current_user)
        datasets = await dao.get_datasets_for_dashboard(dashboard)
        is_guest = security_manager.is_guest_user(current_user)

        def _resolve_generic_type(sqla_type: Any, is_dttm: bool) -> int | None:
            """Map SQLAlchemy column type → utils.GenericDataType int.
            0=NUMERIC, 1=STRING, 2=TEMPORAL, 3=BOOLEAN.  Matches
            ``TableColumn.type_generic`` in superset_old without
            requiring a live db_engine_spec lookup (which needs a
            database connection in async context).
            """
            if is_dttm:
                return 2  # TEMPORAL
            if not sqla_type:
                return None
            t = str(sqla_type).upper()
            if "BOOL" in t:
                return 3
            if any(k in t for k in ("DATE", "TIME", "TIMESTAMP")):
                return 2
            if any(k in t for k in ("CHAR", "TEXT", "STRING", "JSON", "UUID")):
                return 1
            if any(
                k in t
                for k in (
                    "INT",
                    "NUMERIC",
                    "DECIMAL",
                    "FLOAT",
                    "DOUBLE",
                    "REAL",
                    "BIGINT",
                    "SMALLINT",
                )
            ):
                return 0
            return None

        def _build_dataset_dict(ds: Any) -> dict[str, Any]:
            columns = getattr(ds, "columns", []) or []
            metrics = getattr(ds, "metrics", []) or []
            owners = getattr(ds, "owners", []) or []
            database = getattr(ds, "database", None)
            table_name = getattr(ds, "table_name", None) or ""
            ds_id: int = ds.id
            main_dttm_col = getattr(ds, "main_dttm_col", None)

            column_names = [
                c.column_name for c in columns if getattr(c, "column_name", None)
            ]
            # Unique set of generic column types (matches
            # ``superset_old/connectors/sqla/models.py:482``)
            column_types: list[int] = []
            seen_types: set[int] = set()
            for c in columns:
                gt = _resolve_generic_type(
                    getattr(c, "type", None),
                    getattr(c, "is_dttm", False),
                )
                if gt is not None and gt not in seen_types:
                    seen_types.add(gt)
                    column_types.append(gt)
            verbose_map = {
                c.column_name: (getattr(c, "verbose_name", None) or c.column_name)
                for c in columns
                if getattr(c, "column_name", None)
            }
            cols_dicts = [
                {
                    "column_name": c.column_name,
                    "verbose_name": getattr(c, "verbose_name", None),
                    "is_dttm": getattr(c, "is_dttm", False),
                    "type": getattr(c, "type", None),
                    "groupby": getattr(c, "groupby", True),
                    "filterable": getattr(c, "filterable", True),
                    "expression": getattr(c, "expression", None),
                }
                for c in columns
                if getattr(c, "column_name", None)
            ]
            metrics_dicts = [
                {
                    "metric_name": m.metric_name,
                    "verbose_name": getattr(m, "verbose_name", None),
                    "expression": getattr(m, "expression", None),
                }
                for m in metrics
                if getattr(m, "metric_name", None)
            ]
            # ``choicify(self.dttm_cols)`` upstream → ``[[value, label], …]``
            # pairs (superset_old/connectors/sqla/models.py:1346 +
            # DashboardDatasetSchema's ``fields.List(fields.List(...))``); a
            # flat list of names breaks the time-column filter control.
            granularity_sqla = [
                [c.column_name, c.column_name]
                for c in columns
                if getattr(c, "is_dttm", False) and getattr(c, "column_name", None)
            ]
            # Build ``order_by_choices`` in the shape expected by the
            # frontend (json-encoded string + human label). See
            # ``superset/schemas/dataset.py._resolve_order_by_choices``
            # for the full rationale — raw [col, bool] pairs break the
            # table viz ``order_by_cols`` SelectControl hydration.
            import json as _json

            order_by_choices: list[list[Any]] = []
            for c in columns:
                col_name = getattr(c, "column_name", None)
                if col_name:
                    order_by_choices.append(
                        [_json.dumps([col_name, True]), f"{col_name} [asc]"]
                    )
                    order_by_choices.append(
                        [_json.dumps([col_name, False]), f"{col_name} [desc]"]
                    )
            owners_list = [
                {
                    "id": o.id,
                    "first_name": getattr(o, "first_name", ""),
                    "last_name": getattr(o, "last_name", ""),
                }
                for o in owners
            ]
            db_dict = (
                {
                    "id": database.id,
                    "database_name": getattr(database, "database_name", ""),
                    "backend": getattr(database, "backend", None),
                }
                if database
                else None
            )
            return {
                "id": ds_id,
                "uid": f"{ds_id}__table",
                "table_name": table_name,
                "name": table_name,
                "datasource_name": table_name,
                "type": "table",
                "schema": getattr(ds, "schema", None),
                "catalog": getattr(ds, "catalog", None),
                "database": db_dict,
                "filter_select_enabled": getattr(ds, "filter_select_enabled", False),
                "is_sqllab_view": getattr(ds, "is_sqllab_view", False),
                "main_dttm_col": main_dttm_col,
                "offset": getattr(ds, "offset", 0),
                "cache_timeout": getattr(ds, "cache_timeout", None),
                "default_endpoint": getattr(ds, "default_endpoint", None),
                "fetch_values_predicate": getattr(ds, "fetch_values_predicate", None),
                "template_params": getattr(ds, "template_params", None),
                "params": getattr(ds, "params", None),
                "perm": getattr(ds, "perm", None),
                "sql": getattr(ds, "sql", None),
                "extra": getattr(ds, "extra", None),
                "normalize_columns": getattr(ds, "normalize_columns", False),
                "always_filter_main_dttm": getattr(
                    ds, "always_filter_main_dttm", False
                ),
                "edit_url": f"/tablemodelview/edit/{ds_id}",
                "column_names": column_names,
                "column_formats": {},
                "column_types": column_types,
                "verbose_map": verbose_map,
                "columns": cols_dicts,
                "metrics": metrics_dicts,
                "granularity_sqla": granularity_sqla,
                "time_grain_sqla": _get_time_grain_sqla(database),
                "order_by_choices": order_by_choices,
                "owners": owners_list,
                "select_star": None,
                # ``filter_select`` is a legacy alias kept for frontend
                # compatibility (TODO deprecate — see superset_old
                # connectors/sqla/models.py:375).
                "filter_select": getattr(ds, "filter_select_enabled", False),
                "health_check_message": getattr(ds, "health_check_message", None),
            }

        # 1:1 with original DashboardDatasetSchema.post_dump:
        # strip ``owners`` and ``database`` from dataset dicts when the
        # current user is a guest (embedded Superset).
        def _scrub_for_guest(d: dict[str, Any]) -> dict[str, Any]:
            if is_guest:
                d.pop("owners", None)
                d.pop("database", None)
            return d

        return {
            "result": [_scrub_for_guest(_build_dataset_dict(ds)) for ds in datasets]
        }

    # ------------------------------------------------------------------
    # GET — tab structure
    # ------------------------------------------------------------------
    @get(
        "/{id_or_slug:str}/tabs",
        guards=[require_permission("can_read", "Dashboard")],
    )
    async def get_tabs(  # noqa: C901
        self,
        id_or_slug: str,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        import json as _json
        from collections import deque

        # Access-scope like the full GET: ``dashboard_access_filters`` → 404
        # for an unseeable dashboard, ``raise_for_access`` the 403 gate; full
        # loader avoids a MissingGreenlet on owners/roles. Guards the tab
        # structure of an inaccessible dashboard against coarse-``can_read``
        # enumeration.
        from superset.db.filters import dashboard_access_filters

        access_filters = await dashboard_access_filters(security_manager, current_user)
        dashboard = await dao.get_full_by_id_or_slug(
            id_or_slug, extra_filters=access_filters
        )
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", id_or_slug)
        await security_manager.raise_for_access(dashboard=dashboard, user=current_user)

        try:
            position = (
                _json.loads(dashboard.position_json) if dashboard.position_json else {}
            )
        except (ValueError, TypeError) as err:
            from litestar.exceptions import ClientException

            from superset.i18n import gettext as _

            # 1:1 with ``except (TypeError, ValueError) → response_400``
            # (superset_old/dashboards/api.py:484-489) — HTTP 400, not 422.
            raise ClientException(
                status_code=400,
                detail=_(
                    "Tab schema is invalid, caused by: %(error_msg)s",
                    error_msg=str(err),
                ),
            ) from err

        if position == {}:
            # 1:1 with ``Dashboard.tabs`` (superset_old/models/dashboard.py:
            # 307-309): ``if self.position == {}: return {}`` — covers NULL,
            # "" AND the JSON-empty ``"{}"`` string; the dumped result is an
            # EMPTY object (no all_tabs/tab_tree keys).
            return {"result": {}}

        all_tabs: dict[str, str] = {}
        tab_tree: list[dict[str, Any]] = []

        # Direct dict indexing throughout — 1:1 with ``Dashboard.tabs``
        # (superset_old/models/dashboard.py:311-342): a position_json that is
        # valid JSON but lacks ROOT_ID / a node "type" / "meta"."text" raises
        # KeyError, which the original API's ``except (TypeError, ValueError)``
        # does NOT catch -> @safe -> HTTP 500. Defensive ``.get`` fallbacks
        # here turned those into silent 200s.
        def get_node(node_id: str) -> dict[str, Any]:
            return position[node_id]

        def build_tab_tree(
            node: dict[str, Any], children: list[dict[str, Any]]
        ) -> None:
            new_children: list[dict[str, Any]] = []
            for child_id in node.get("children", []):
                child = get_node(child_id)
                if node["type"] == "TABS":
                    children.append(child)
                    queue.append((child, new_children))
                elif node["type"] in ("GRID", "ROOT"):
                    queue.append((child, children))
                elif node["type"] == "TAB":
                    queue.append((child, new_children))
            if node["type"] == "TAB":
                node["children"] = new_children
                node["title"] = node["meta"]["text"]
                node["value"] = node["id"]
                all_tabs[node["id"]] = node["title"]

        root = get_node("ROOT_ID")

        queue: deque[tuple[dict[str, Any], list[dict[str, Any]]]] = deque()
        queue.append((root, tab_tree))
        while queue:
            node, children = queue.popleft()
            build_tab_tree(node, children)

        return {"result": {"all_tabs": all_tabs, "tab_tree": tab_tree}}

    # ------------------------------------------------------------------
    # GET — related charts
    # ------------------------------------------------------------------
    @get(
        "/{id_or_slug:str}/charts",
        guards=[require_permission("can_read", "Dashboard")],
    )
    async def get_charts(
        self,
        id_or_slug: str,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        # Access-scope like the full GET: ``dashboard_access_filters`` → 404
        # for an unseeable dashboard, ``raise_for_access`` the 403 gate; full
        # loader avoids a MissingGreenlet on owners/roles. Without this, any
        # user with the coarse ``can_read Dashboard`` perm could enumerate the
        # chart definitions (form_data incl. metrics/SQL) of a dashboard they
        # cannot access.
        from superset.db.filters import dashboard_access_filters

        access_filters = await dashboard_access_filters(security_manager, current_user)
        dashboard = await dao.get_full_by_id_or_slug(
            id_or_slug, extra_filters=access_filters
        )
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", id_or_slug)
        await security_manager.raise_for_access(dashboard=dashboard, user=current_user)
        charts = await dao.get_charts_for_dashboard(dashboard)

        from superset.utils.core import markdown as _markdown

        result = []
        for chart in charts:
            # Use Slice.form_data property — applies update_time_range()
            # which migrates since/until → time_range.
            fd = chart.form_data

            desc = getattr(chart, "description", None) or ""
            result.append(
                {
                    "id": chart.id,
                    "slice_name": chart.slice_name,
                    "cache_timeout": getattr(chart, "cache_timeout", None),
                    "changed_on": (
                        chart.changed_on.isoformat()
                        if getattr(chart, "changed_on", None)
                        else None
                    ),
                    "description": desc or None,
                    # 1:1 with Slice.description_markeddown
                    # (superset_old/models/slice.py:215-216).
                    "description_markeddown": _markdown(desc),
                    "form_data": fd,
                    "slice_url": getattr(chart, "slice_url", None),
                    "certified_by": getattr(chart, "certified_by", None),
                    "certification_details": getattr(
                        chart, "certification_details", None
                    ),
                }
            )
        return {"result": result}

    # ------------------------------------------------------------------
    # POST — create
    # ------------------------------------------------------------------
    @post(
        "/",
        guards=[require_permission("can_write", "Dashboard")],
        status_code=201,
    )
    async def create(
        self,
        data: DashboardPostSchema,
        dao: DashboardDAOProtocol,
        current_user: UserProtocol,
        security_manager: SecurityManagerProtocol,
    ) -> DashboardGetResponse:
        cmd = CreateDashboardCommand(
            dao=cast("AsyncDashboardDAO", dao),
            data=filter_none(
                {
                    "dashboard_title": data.dashboard_title,
                    "slug": data.slug,
                    "position_json": data.position_json,
                    "css": data.css,
                    "json_metadata": data.json_metadata,
                    "published": data.published,
                    "certified_by": data.certified_by,
                    "certification_details": data.certification_details,
                    "is_managed_externally": data.is_managed_externally,
                    "external_url": data.external_url,
                    "owners": data.owners,
                    "roles": data.roles,
                    "tags": data.tags,
                    "theme_id": data.theme_id,
                    "uuid": data.uuid,
                }
            ),
            user_id=current_user.id,
            security_manager=security_manager,
        )
        dashboard = await cmd.execute()
        dashboard_id = int(dashboard.id)
        await event_logger.alog_with_context(
            "dashboard.create",
            object_ref=f"dashboard:{dashboard_id}",
            user_id=current_user.id,
        )
        return DashboardGetResponse(
            id=dashboard_id,
            result=DashboardDetailResult.from_model_brief(dashboard),
        )

    # ------------------------------------------------------------------
    # PUT — update
    # ------------------------------------------------------------------
    @put(
        "/{pk:int}",
        guards=[require_permission("can_write", "Dashboard")],
    )
    async def update(
        self,
        pk: int,
        data: DashboardPutSchema,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> DashboardGetResponse:
        update_data = filter_unset(
            {
                "dashboard_title": data.dashboard_title,
                "slug": data.slug,
                "position_json": data.position_json,
                "css": data.css,
                "json_metadata": data.json_metadata,
                "published": data.published,
                "certified_by": data.certified_by,
                "certification_details": data.certification_details,
                "is_managed_externally": data.is_managed_externally,
                "external_url": data.external_url,
                "owners": data.owners,
                "roles": data.roles,
                "tags": data.tags,
                "theme_id": data.theme_id,
                "uuid": data.uuid,
            }
        )
        cmd = UpdateDashboardCommand(
            dao=cast("AsyncDashboardDAO", dao),
            dashboard_id=pk,
            data=update_data,
            security_manager=security_manager,
            user_id=current_user.id,
        )
        dashboard = await cmd.execute()

        # Eager-load relationships for serialization (avoids lazy load on async)
        from sqlalchemy.orm import selectinload

        from superset.models.dashboard import Dashboard

        refreshed = await dao.find_by_id_with_options(
            dashboard_id=dashboard.id,
            options=[
                selectinload(Dashboard.owners),
                selectinload(Dashboard.roles),
                selectinload(Dashboard.tags),
                selectinload(Dashboard.changed_by),
                selectinload(Dashboard.created_by),
                selectinload(Dashboard.slices),
                selectinload(Dashboard.theme),
            ],
        )
        assert refreshed is not None  # just updated, must exist
        dashboard = refreshed

        await event_logger.alog_with_context(
            "dashboard.update",
            object_ref=f"dashboard:{pk}",
            user_id=current_user.id,
        )
        changed_on = getattr(dashboard, "changed_on", None)
        last_modified_time = (
            changed_on.replace(microsecond=0).timestamp() if changed_on else None
        )
        return DashboardGetResponse(
            id=int(dashboard.id),
            result=DashboardDetailResult.from_model(dashboard),
            last_modified_time=last_modified_time,
        )

    # ------------------------------------------------------------------
    # PUT — update filters
    # ------------------------------------------------------------------
    @put(
        "/{pk:int}/filters",
        guards=[require_permission("can_write", "Dashboard")],
    )
    async def update_filters(
        self,
        pk: int,
        data: DashboardFiltersUpdateSchema,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        cmd = UpdateDashboardFiltersCommand(
            dao=cast("AsyncDashboardDAO", dao),
            dashboard_id=pk,
            data={
                "deleted": data.deleted,
                "modified": data.modified,
                "reordered": data.reordered,
            },
            security_manager=security_manager,
            user_id=current_user.id,
        )
        updated_configuration = await cmd.execute()
        await event_logger.alog_with_context(
            "dashboard.update_filters",
            object_ref=f"dashboard:{pk}",
            user_id=current_user.id,
        )
        return {"result": updated_configuration}

    # ------------------------------------------------------------------
    # PUT — update colors
    # ------------------------------------------------------------------
    @put(
        "/{pk:int}/colors",
        guards=[require_permission("can_write", "Dashboard")],
    )
    async def update_colors(
        self,
        pk: int,
        data: DashboardColorsUpdateSchema,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        mark_updated: bool = Parameter(query="mark_updated", default=True),  # noqa: B008
    ) -> Response[None]:
        cmd = UpdateDashboardColorsCommand(
            dao=cast("AsyncDashboardDAO", dao),
            dashboard_id=pk,
            data={
                "color_scheme": data.color_scheme,
                "label_colors": data.label_colors,
                "map_label_colors": data.map_label_colors,
                "shared_label_colors": data.shared_label_colors,
                "color_scheme_domain": data.color_scheme_domain,
            },
            security_manager=security_manager,
            user_id=current_user.id,
            mark_updated=mark_updated,
        )
        await cmd.execute()
        await event_logger.alog_with_context(
            "dashboard.update_colors",
            object_ref=f"dashboard:{pk}",
            user_id=current_user.id,
        )
        return Response(content=None, status_code=200)

    # ------------------------------------------------------------------
    # DELETE — single
    # ------------------------------------------------------------------
    @delete(
        "/{pk:int}",
        guards=[require_permission("can_write", "Dashboard")],
        status_code=200,
    )
    async def delete_dashboard(
        self,
        pk: int,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, str]:
        cmd = DeleteDashboardCommand(
            dao=cast("AsyncDashboardDAO", dao),
            dashboard_id=pk,
            security_manager=security_manager,
            user_id=current_user.id,
        )
        await cmd.execute()
        await event_logger.alog_with_context(
            "dashboard.delete",
            object_ref=f"dashboard:{pk}",
            user_id=current_user.id,
        )
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # DELETE — bulk
    # ------------------------------------------------------------------
    @delete(
        "/",
        guards=[require_permission("can_write", "Dashboard")],
        status_code=200,
    )
    async def bulk_delete(
        self,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        rison_params: list[int] | dict[str, Any] | None,
    ) -> dict[str, str]:
        ids = extract_ids_required(rison_params)
        cmd = BulkDeleteDashboardsCommand(
            dao=cast("AsyncDashboardDAO", dao),
            dashboard_ids=ids,
            security_manager=security_manager,
            user_id=current_user.id,
        )
        await cmd.execute()
        await event_logger.alog_with_context(
            "dashboard.bulk_delete",
            user_id=current_user.id,
            extra={"count": len(ids)},
        )
        num = len(ids)
        msg = f"Deleted {num} dashboard" if num == 1 else f"Deleted {num} dashboards"
        return {"message": msg}

    # ------------------------------------------------------------------
    # GET — export (ZIP)
    # ------------------------------------------------------------------
    @get(
        "/export/",
        guards=[require_permission("can_export", "Dashboard")],
        media_type="application/zip",
    )
    async def export(
        self,
        dao: DashboardDAOProtocol,
        rison_params: list[int] | dict[str, Any] | None,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        token: str | None = Parameter(query="token", default=None),
    ) -> Stream:
        ids = extract_ids(rison_params)
        if not ids:
            raise CommandInvalidError("At least one ID is required for export")
        # 1:1 with ``superset_old/dashboards/api.py:1008-1031``: the ZIP is named
        # ``dashboard_export_{YYYYMMDDTHHMMSS}.zip`` AND every entry inside is
        # nested under that same ``dashboard_export_{ts}/`` folder — so the v1
        # importer's ``remove_root`` (parts[1:]) strips it back off and the
        # re-import round-trip works. The root-folder wrapping was missing →
        # ``remove_root("metadata.yaml")`` returned ``"."`` → "Missing
        # metadata.yaml" on re-import.
        from datetime import datetime as _datetime

        timestamp = _datetime.now().strftime("%Y%m%dT%H%M%S")
        root = f"dashboard_export_{timestamp}"
        cmd = ExportDashboardsCommand(
            model_ids=ids,
            dao=cast("AsyncDashboardDAO", dao),
            security_manager=security_manager,
            user=current_user,
        )
        cmd._root = root  # noqa: SLF001
        buf = await cmd.execute()
        await event_logger.alog_with_context(
            "dashboard.export", extra={"count": len(ids)}
        )
        # When a ``token`` query param is present — a cookie *named by the
        # token value* is set to ``done`` (``response.set_cookie(token, "done",
        # max_age=600)``) so the frontend can detect download completion. The
        # shared ``build_export_headers`` helper hard-codes both the filename
        # and the cookie name, so we build the headers inline here to preserve
        # the original contract.
        filename = f"{root}.zip"
        headers: dict[str, str] = {
            "Content-Disposition": f"attachment; filename={filename}",
        }
        if token:
            headers["Set-Cookie"] = f"{token}=done; Path=/; Max-Age=600; SameSite=Lax"
        return Stream(
            stream_zip(buf),
            status_code=200,
            media_type="application/zip",
            headers=headers,
        )

    # ------------------------------------------------------------------
    # POST — trigger screenshot
    # ------------------------------------------------------------------
    @post(
        "/{pk:int}/cache_dashboard_screenshot/",
        # 1:1 with original ``superset_old/dashboards/api.py:1034`` which
        # gates this endpoint on the granular ``can_cache_dashboard_screenshot``
        # permission via ``method_permission_name`` mapping.
        guards=[require_permission("can_cache_dashboard_screenshot", "Dashboard")],
    )
    async def cache_dashboard_screenshot(
        self,
        pk: int,
        data: DashboardScreenshotSchema,
        dao: DashboardDAOProtocol,
        kv_dao: KeyValueDAOProtocol,
        state: State,
        current_user: UserProtocol,
        security_manager: SecurityManagerProtocol,
        rison_params: dict[str, Any] | None,
    ) -> Response[Any]:
        """Compute and cache a dashboard screenshot.

        1:1 port of ``superset_old/dashboards/api.py:cache_dashboard_screenshot``.
        Mints or reuses a permalink key, computes the canonical cache key
        (including the permalink) and dispatches the Celery screenshot task.
        Returns 200 on cache hit, 202 when a new task is queued.
        """
        import asyncio

        settings = getattr(state, "settings", None)
        flags = getattr(settings, "feature_flags", {}) or {}
        if not flags.get("THUMBNAILS", False) or not flags.get(
            "ENABLE_DASHBOARD_SCREENSHOT_ENDPOINTS", False
        ):
            raise ObjectNotFoundError("Dashboard screenshot", pk)
        # Access-scoped lookup (1:1 upstream ``datamodel.get(pk, base_filters)``
        # → 404) — without it any holder of the coarse perm could serve/trigger
        # a screenshot or thumbnail of a dashboard they cannot access.
        from superset.db.filters import dashboard_access_filters

        _dash_filters = await dashboard_access_filters(security_manager, current_user)
        dashboard = await dao.get_full_by_id_or_slug(
            str(pk), extra_filters=_dash_filters
        )
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", pk)

        # Extract rison query params (window_size, thumb_size, force)
        rison_dict: dict[str, Any] = rison_params or {}
        force: bool = bool(rison_dict.get("force", False))
        window_size = rison_dict.get("window_size") or (1600, 1200)
        # Don't shrink the image if thumb_size is not specified
        thumb_size = rison_dict.get("thumb_size") or window_size

        # Build dashboard_state from POST body (camelCase / snake_case dual support)
        dashboard_state: dict[str, Any] = {
            "dataMask": (
                getattr(data, "data_mask", None)
                or getattr(data, "dataMask", None)
                or {}
            ),
            "activeTabs": (
                getattr(data, "active_tabs", None)
                or getattr(data, "activeTabs", None)
                or []
            ),
            "anchor": getattr(data, "anchor", None) or "",
            "urlParams": (
                getattr(data, "url_params", None)
                or getattr(data, "urlParams", None)
                or []
            ),
        }

        # If the client already has a permalink key, reuse it; otherwise mint
        # a new one so the screenshot is tied to the correct dashboard state.
        permalink_key = getattr(data, "permalink_key", None) or getattr(
            data, "permalinkKey", None
        )
        if not permalink_key:
            # kv_dao is an AsyncKeyValueDAO instance wired via provider
            _kv = cast("AsyncKeyValueDAO", kv_dao)
            permalink_key = await CreateDashboardPermalinkCommand(
                dao=_kv,
                dashboard_id=pk,
                state=dashboard_state,
                dashboard_uuid=str(getattr(dashboard, "uuid", "") or ""),
                user_id=getattr(current_user, "id", None),
            ).run()

        dashboard_url = f"/superset/dashboard/p/{permalink_key}/"

        from superset.utils.screenshots import (
            DashboardScreenshot,
            ScreenshotCachePayload,
        )

        dashboard_digest = getattr(dashboard, "digest", None) or str(pk)
        screenshot_obj = DashboardScreenshot(dashboard_url, dashboard_digest)
        cache_key = await asyncio.to_thread(
            screenshot_obj.get_cache_key, window_size, thumb_size, permalink_key
        )
        cache_payload = (
            await asyncio.to_thread(DashboardScreenshot.get_from_cache_key, cache_key)
            or ScreenshotCachePayload()
        )
        image_url = f"/api/v1/dashboard/{pk}/screenshot/{cache_key}/"

        def _build_response(status_code: int) -> Response[Any]:
            return Response(
                content={
                    "cache_key": cache_key,
                    "dashboard_url": dashboard_url,
                    "image_url": image_url,
                    "task_updated_at": cache_payload.get_timestamp(),
                    "task_status": cache_payload.get_status(),
                },
                status_code=status_code,
                media_type="application/json",
            )

        if cache_payload.should_trigger_task(force):
            await asyncio.to_thread(
                screenshot_obj.cache.set,
                cache_key,
                ScreenshotCachePayload().to_dict(),
            )
            from superset.tasks.thumbnails import (
                cache_dashboard_screenshot as _cache_dashboard_screenshot_task,
            )

            # Extract guest_token for embedded (guest user) flow
            guest_token: dict[str, Any] | None = None
            from superset.security.guest import GuestUser

            if isinstance(current_user, GuestUser):
                guest_token = getattr(current_user, "token_payload", None)

            _cache_dashboard_screenshot_task.delay(
                username=getattr(current_user, "username", None),
                dashboard_id=pk,
                dashboard_url=dashboard_url,
                force=force,
                cache_key=cache_key,
                guest_token=guest_token,
                thumb_size=thumb_size,
                window_size=window_size,
            )
            return _build_response(202)
        return _build_response(200)

    # ------------------------------------------------------------------
    # GET — screenshot
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}/screenshot/{digest:str}/",
        guards=[require_permission("can_read", "Dashboard")],
        media_type="image/png",
    )
    async def screenshot(
        self,
        pk: int,
        digest: str,
        dao: DashboardDAOProtocol,
        state: State,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        request: Request,  # type: ignore[type-arg]
    ) -> Response[Any]:
        """Get a computed dashboard screenshot from cache.

        The *digest* path parameter is the cache key.  If the cache
        contains an image we serve it (as PNG or converted to PDF
        depending on the ``download_format`` query parameter);
        otherwise we return 404.
        """
        import asyncio

        # Check feature flag BEFORE importing screenshots — that module's
        # import chain pulls in webdriver/playwright/selenium which can
        # blow up in deployments where the dependencies are not present.
        # When THUMBNAILS is disabled we should respond 404 without ever
        # touching the optional code path.
        settings = getattr(state, "settings", None)
        flags = getattr(settings, "feature_flags", {}) or {}
        if not flags.get("THUMBNAILS", False) or not flags.get(
            "ENABLE_DASHBOARD_SCREENSHOT_ENDPOINTS", False
        ):
            raise ObjectNotFoundError("Dashboard screenshot", pk)

        # Access-scoped lookup (1:1 upstream ``datamodel.get(pk, base_filters)``
        # → 404) — without it any holder of the coarse perm could serve/trigger
        # a screenshot or thumbnail of a dashboard they cannot access.
        from superset.db.filters import dashboard_access_filters
        from superset.utils.screenshots import (
            DashboardScreenshot,
            ScreenshotImageNotAvailableException,
        )

        _dash_filters = await dashboard_access_filters(security_manager, current_user)
        dashboard = await dao.get_full_by_id_or_slug(
            str(pk), extra_filters=_dash_filters
        )
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", pk)

        download_format = request.query_params.get("download_format", "png")

        cache_payload = await asyncio.to_thread(
            DashboardScreenshot.get_from_cache_key, digest
        )
        if cache_payload is not None:
            try:
                image = cache_payload.get_image()
            except ScreenshotImageNotAvailableException:
                # JSON (not empty image/png) on miss — 1:1 with the original
                # ``response_404()``; see the ImageLoader note on the thumbnail
                # endpoint below.
                return Response(
                    content={"message": "Not found"},
                    status_code=404,
                    media_type="application/json",
                )

            if download_format == "pdf":
                from superset.utils.pdf import build_pdf_from_screenshots

                pdf_data = build_pdf_from_screenshots([image.getvalue()])
                return Response(
                    content=pdf_data,
                    status_code=200,
                    media_type="application/pdf",
                    headers={
                        "Content-Disposition": "inline; filename=dashboard.pdf",
                    },
                )
            # Default: PNG
            return Response(
                content=image.getvalue(),
                status_code=200,
                media_type="image/png",
            )
        return Response(
            content={"message": "Not found"},
            status_code=404,
            media_type="application/json",
        )

    # ------------------------------------------------------------------
    # GET — thumbnail
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}/thumbnail/{digest:str}/",
        guards=[require_permission("can_read", "Dashboard")],
        media_type="image/png",
    )
    async def thumbnail(
        self,
        pk: int,
        digest: str,
        dao: DashboardDAOProtocol,
        state: State,
        current_user: UserProtocol,
        security_manager: SecurityManagerProtocol,
    ) -> Response[Any]:
        """Compute or get already computed dashboard thumbnail from cache.

        If the dashboard's current digest differs from *digest* we redirect
        to the canonical URL.  Otherwise we check the thumbnail cache: if
        the image exists we serve it directly; if not we queue a Celery task
        and return 202.
        """
        import asyncio

        from litestar.response import Redirect

        # Gate on the feature flag *before* importing screenshots so the
        # optional webdriver dependency chain is never touched when
        # thumbnails are disabled (see screenshot endpoint above).
        settings = getattr(state, "settings", None)
        flags = getattr(settings, "feature_flags", {}) or {}
        if not flags.get("THUMBNAILS", False):
            raise ObjectNotFoundError("Dashboard thumbnail", pk)

        # Access-scoped lookup (1:1 upstream ``datamodel.get(pk, base_filters)``
        # → 404) — without it any holder of the coarse perm could serve/trigger
        # a screenshot or thumbnail of a dashboard they cannot access.
        from superset.db.filters import dashboard_access_filters
        from superset.utils.screenshots import (
            DashboardScreenshot,
            ScreenshotCachePayload,
            ScreenshotImageNotAvailableException,
        )

        _dash_filters = await dashboard_access_filters(security_manager, current_user)
        dashboard = await dao.get_full_by_id_or_slug(
            str(pk), extra_filters=_dash_filters
        )
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", pk)

        # Redirect to canonical digest URL if stale
        dashboard_digest = getattr(dashboard, "digest", None)
        if dashboard_digest and dashboard_digest != digest:
            return Redirect(
                path=f"/api/v1/dashboard/{pk}/thumbnail/{dashboard_digest}/",
            )

        # Build screenshot object and compute cache key
        dashboard_url = f"/superset/dashboard/{pk}/"
        screenshot_obj = DashboardScreenshot(dashboard_url, dashboard_digest or digest)
        cache_key = await asyncio.to_thread(screenshot_obj.get_cache_key)
        cache_payload = (
            await asyncio.to_thread(DashboardScreenshot.get_from_cache_key, cache_key)
            or ScreenshotCachePayload()
        )

        # NB: every non-image response below returns JSON (``image/png`` ONLY on
        # a real image) — 1:1 with the original (``response(202, ...)`` /
        # ``response_404()``). The frontend ``ImageLoader`` fetches this URL and
        # tests ``/image/.test(blob.type)``; an empty ``image/png`` body would
        # pass that test and render a blank tile instead of the fallback.
        if cache_payload.should_trigger_task():
            # Mark as pending in cache and dispatch Celery task
            await asyncio.to_thread(
                screenshot_obj.cache.set,
                cache_key,
                ScreenshotCachePayload().to_dict(),
            )
            from superset.tasks.thumbnails import cache_dashboard_thumbnail

            cache_dashboard_thumbnail.delay(
                current_user=getattr(current_user, "username", None),
                dashboard_id=dashboard.id,
                force=False,
                cache_key=cache_key,
            )
            # 1:1 with the original ``response(202, cache_key=..., dashboard_url=
            # ..., image_url=..., task_updated_at=..., task_status=...)``
            # (dashboards/api.py:1320).
            return Response(
                content={
                    "cache_key": cache_key,
                    "dashboard_url": dashboard_url,
                    "image_url": f"/api/v1/dashboard/{pk}/thumbnail/{cache_key}/",
                    "task_updated_at": cache_payload.get_timestamp(),
                    "task_status": cache_payload.get_status(),
                },
                status_code=202,
                media_type="application/json",
            )

        # Serve from cache
        try:
            image = cache_payload.get_image()
            # Validate the BytesIO object
            if not image or not hasattr(image, "read"):
                return Response(
                    content={"message": "Not found"},
                    status_code=404,
                    media_type="application/json",
                )
            if image.getbuffer().nbytes == 0:
                return Response(
                    content={"message": "Not found"},
                    status_code=404,
                    media_type="application/json",
                )
            image.seek(0)
        except ScreenshotImageNotAvailableException:
            return Response(
                content={"message": "Not found"},
                status_code=404,
                media_type="application/json",
            )
        except Exception:  # noqa: BLE001
            # 1:1 with the original's broad catch
            # (superset_old/dashboards/api.py:1350-1357): any unexpected
            # error retrieving the cached image → clean 404, not a 500.
            logger.error("Error fetching dashboard thumbnail", exc_info=True)
            return Response(
                content={"message": "Not found"},
                status_code=404,
                media_type="application/json",
            )
        return Response(
            content=image.read(),
            status_code=200,
            media_type="image/png",
        )

    # ------------------------------------------------------------------
    # GET — favorite status
    # ------------------------------------------------------------------
    @get(
        "/favorite_status/",
        guards=[require_permission("can_read", "Dashboard")],
    )
    async def favorite_status(
        self,
        dao: DashboardDAOProtocol,
        current_user: UserProtocol,
        rison_params: list[int] | dict[str, Any] | None,
    ) -> FavoriteStatusResponse:
        ids = extract_ids(rison_params)
        if not ids:
            return FavoriteStatusResponse()
        # 1:1 with superset_old/dashboards/api.py:1404-1406 — resolve the
        # requested ids first and 404 when none of them exist.
        dashboards = await dao.find_by_ids(ids)
        if not dashboards:
            raise ObjectNotFoundError("Dashboard")
        fav_ids = set(await dao.favorited_ids(ids, current_user.id))
        return FavoriteStatusResponse(
            result=[FavoriteStatusItem(id=i, value=i in fav_ids) for i in ids],
        )

    # ------------------------------------------------------------------
    # POST — add favorite
    # ------------------------------------------------------------------
    @post(
        "/{pk:int}/favorites/",
        guards=[require_permission("can_read", "Dashboard")],
        status_code=200,
    )
    async def add_favorite(
        self,
        pk: int,
        dao: DashboardDAOProtocol,
        current_user: UserProtocol,
        security_manager: SecurityManagerProtocol,
    ) -> dict[str, str]:
        # 1:1 with original: AddFavoriteDashboardCommand loads via the
        # access-aware path and denies (403) when the user cannot access the
        # dashboard — enforced here via ``can_access_dashboard``.
        cmd = AddFavoriteDashboardCommand(
            dao=cast("AsyncDashboardDAO", dao),
            dashboard_id=pk,
            user_id=current_user.id,
            security_manager=security_manager,
            user=current_user,
        )
        await cmd.execute()
        await event_logger.alog_with_context(
            "dashboard.add_favorite",
            object_ref=f"dashboard:{pk}",
            user_id=current_user.id,
        )
        return {"result": "OK"}

    # ------------------------------------------------------------------
    # DELETE — remove favorite
    # ------------------------------------------------------------------
    @delete(
        "/{pk:int}/favorites/",
        guards=[require_permission("can_read", "Dashboard")],
        status_code=200,
    )
    async def remove_favorite(
        self,
        pk: int,
        dao: DashboardDAOProtocol,
        current_user: UserProtocol,
        security_manager: SecurityManagerProtocol,
    ) -> dict[str, str]:
        # 1:1 with original: RemoveFavoriteDashboardCommand enforces dashboard
        # access (403) before unfavoriting via ``can_access_dashboard``.
        cmd = RemoveFavoriteDashboardCommand(
            dao=cast("AsyncDashboardDAO", dao),
            dashboard_id=pk,
            user_id=current_user.id,
            security_manager=security_manager,
            user=current_user,
        )
        await cmd.execute()
        await event_logger.alog_with_context(
            "dashboard.remove_favorite",
            object_ref=f"dashboard:{pk}",
            user_id=current_user.id,
        )
        return {"result": "OK"}

    # ------------------------------------------------------------------
    # POST — import
    # ------------------------------------------------------------------
    @post(
        "/import/",
        guards=[require_permission("can_write", "Dashboard")],
        media_type="application/json",
        # Upstream returns 200 "OK" (dashboards/api.py import_); align.
        status_code=200,
    )
    async def import_dashboard(
        self,
        request: Request,  # type: ignore[type-arg]
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, str]:
        # Read the multipart body manually (see parse_import_request): the
        # ``data: UploadFile = Body(MULTI_PART)`` injection 500'd when no file
        # field was present (Litestar StopIteration). Missing upload -> 4xx.
        (
            _buf,
            filename,
            overwrite,
            passwords_dict,
            ssh_dict,
            ssh_private_keys_dict,
            ssh_private_key_passwords_dict,
        ) = await parse_import_request(request)
        contents = _buf.getvalue()

        # Mirror ``superset_old/dashboards/api.py:1587-1628``: a ZIP bundle is
        # parsed (remove_root + YAML filter) and dispatched v1-then-v0; a
        # non-ZIP upload is a single legacy (v0) JSON document. The dispatcher
        # (``superset/importexport/legacy/dispatcher.py``) tries the async v1
        # command first and falls back to the sync v0 command on
        # ``IncorrectVersionError`` — matching the original
        # ``commands/dashboard/importers/dispatcher.py``.
        parsed, is_zip = _parse_import_upload(filename, contents)
        if is_zip:
            dispatcher = LegacyImportDashboardsDispatcher(
                parsed,
                overwrite=overwrite,
                passwords=passwords_dict,
                ssh_tunnel_passwords=ssh_dict,
                ssh_tunnel_private_keys=ssh_private_keys_dict,
                ssh_tunnel_private_key_passwords=ssh_private_key_passwords_dict,
                # Thread the importing user into the v1 command so imported
                # dashboards get an owner (``get_user()`` upstream) and the
                # overwrite-access check runs — 1:1 with upstream's
                # ``v1/utils.py`` which resolves ``user`` internally. Without
                # this, imports were ownerless and overwrite bypassed access.
                # (v0 ignores extra kwargs.)
                security_manager=security_manager,
                current_user=current_user,
            )
            await dispatcher.run_async(dao=cast("AsyncDashboardDAO", dao))
        else:
            # A single JSON document is unversioned (v0). The modern v1
            # importer always requires a ZIP with ``metadata.yaml``, so route
            # straight to the sync v0 legacy command (run in a worker thread
            # because it uses a sync ``Session``), matching the v0 fallback the
            # original dispatcher reaches in this path.
            import asyncio as _asyncio

            from superset.importexport.legacy.dashboard_v0 import (
                ImportDashboardsCommand as V0ImportDashboardsCommand,
            )

            await _asyncio.to_thread(
                V0ImportDashboardsCommand(parsed).run,
            )
        await event_logger.alog_with_context("dashboard.import")
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # GET — embedded config
    # ------------------------------------------------------------------
    @get(
        "/{id_or_slug:str}/embedded",
        guards=[
            deny_anon_with_404,
            require_permission("can_read", "Dashboard"),
        ],
    )
    async def get_embedded(
        self,
        id_or_slug: str,
        dao: DashboardDAOProtocol,
        embedded_dao: EmbeddedDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        # Access gate — 1:1 with upstream ``@with_dashboard`` (403/404). Without
        # it any ``can_read Dashboard`` user could read the embedded config
        # (uuid + allowed_domains) of a dashboard they cannot access.
        from superset.db.filters import dashboard_access_filters

        access_filters = await dashboard_access_filters(security_manager, current_user)
        dashboard = await dao.get_full_by_id_or_slug(
            id_or_slug, extra_filters=access_filters
        )
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", id_or_slug)
        await security_manager.raise_for_access(dashboard=dashboard, user=current_user)
        embedded = await embedded_dao.find_by_dashboard_id(dashboard.id)
        if not embedded:
            # 1:1 with ``superset_old/dashboards/api.py:1667-1668``:
            # ``if not dashboard.embedded: return self.response(404)``
            raise ObjectNotFoundError("EmbeddedDashboard", dashboard.id)
        return {
            "result": EmbeddedDashboardResponse(
                uuid=str(embedded.uuid),
                allowed_domains=embedded.allowed_domains or [],
                dashboard_id=str(dashboard.id),
                changed_on=str(embedded.changed_on) if embedded.changed_on else None,
            )
        }

    # ------------------------------------------------------------------
    # POST — create/update embedded
    # ------------------------------------------------------------------
    @post(
        "/{id_or_slug:str}/embedded",
        # 1:1 with original ``superset_old/dashboards/api.py:1678`` — gated
        # on the granular ``can_set_embedded`` permission.
        guards=[require_permission("can_set_embedded", "Dashboard")],
        status_code=200,
    )
    async def create_embedded(
        self,
        id_or_slug: str,
        data: EmbeddedDashboardConfig,
        dao: DashboardDAOProtocol,
        embedded_dao: EmbeddedDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        # Per-object access check — 1:1 with upstream ``@with_dashboard``
        # (set_embedded → get_by_id_or_slug → access filter + raise_for_access).
        # ``can_set_embedded`` alone gates the route, but the dashboard itself
        # must still be accessible (defense-in-depth if the perm is ever granted
        # to a non-admin role).
        from superset.db.filters import dashboard_access_filters

        access_filters = await dashboard_access_filters(security_manager, current_user)
        dashboard = await dao.get_full_by_id_or_slug(
            id_or_slug, extra_filters=access_filters
        )
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", id_or_slug)
        await security_manager.raise_for_access(dashboard=dashboard, user=current_user)
        cmd = UpsertEmbeddedDashboardCommand(
            dao=cast("AsyncDashboardDAO", dao),
            embedded_dao=cast("AsyncEmbeddedDashboardDAO", embedded_dao),
            dashboard_id=dashboard.id,
            allowed_domains=data.allowed_domains,
        )
        embedded = await cmd.execute()
        await event_logger.alog_with_context(
            "dashboard.create_embedded",
            object_ref=f"dashboard:{id_or_slug}",
        )
        _raw = embedded.allow_domain_list
        _domains: list[str] = [d for d in (_raw or "").split(",") if d]
        return {
            "result": EmbeddedDashboardResponse(
                uuid=str(embedded.uuid),
                allowed_domains=_domains,
                dashboard_id=str(dashboard.id),
            )
        }

    # ------------------------------------------------------------------
    # PUT — update embedded (idempotent)
    # ------------------------------------------------------------------
    @put(
        "/{id_or_slug:str}/embedded",
        # 1:1 with original ``superset_old/dashboards/api.py:1678`` — gated
        # on the granular ``can_set_embedded`` permission.
        guards=[require_permission("can_set_embedded", "Dashboard")],
        status_code=200,
    )
    async def update_embedded(
        self,
        id_or_slug: str,
        data: EmbeddedDashboardConfig,
        dao: DashboardDAOProtocol,
        embedded_dao: EmbeddedDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        # Per-object access check — see create_embedded note (1:1 @with_dashboard).
        from superset.db.filters import dashboard_access_filters

        access_filters = await dashboard_access_filters(security_manager, current_user)
        dashboard = await dao.get_full_by_id_or_slug(
            id_or_slug, extra_filters=access_filters
        )
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", id_or_slug)
        await security_manager.raise_for_access(dashboard=dashboard, user=current_user)
        cmd = UpsertEmbeddedDashboardCommand(
            dao=cast("AsyncDashboardDAO", dao),
            embedded_dao=cast("AsyncEmbeddedDashboardDAO", embedded_dao),
            dashboard_id=dashboard.id,
            allowed_domains=data.allowed_domains,
        )
        embedded = await cmd.execute()
        await event_logger.alog_with_context(
            "dashboard.update_embedded",
            object_ref=f"dashboard:{id_or_slug}",
        )
        _raw2 = embedded.allow_domain_list
        _domains2: list[str] = [d for d in (_raw2 or "").split(",") if d]
        return {
            "result": EmbeddedDashboardResponse(
                uuid=str(embedded.uuid),
                allowed_domains=_domains2,
                dashboard_id=str(dashboard.id),
            )
        }

    # ------------------------------------------------------------------
    # DELETE — embedded
    # ------------------------------------------------------------------
    @delete(
        "/{id_or_slug:str}/embedded",
        # 1:1 with original ``superset_old/dashboards/api.py:1761`` —
        # ``@permission_name("set_embedded")`` gates this on the granular
        # ``can_set_embedded`` permission (not ``can_write``).
        guards=[require_permission("can_set_embedded", "Dashboard")],
        status_code=200,
    )
    async def delete_embedded(
        self,
        id_or_slug: str,
        dao: DashboardDAOProtocol,
        embedded_dao: EmbeddedDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, str]:
        # Per-object access check — see create_embedded note (1:1 @with_dashboard).
        from superset.db.filters import dashboard_access_filters

        access_filters = await dashboard_access_filters(security_manager, current_user)
        dashboard = await dao.get_full_by_id_or_slug(
            id_or_slug, extra_filters=access_filters
        )
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", id_or_slug)
        await security_manager.raise_for_access(dashboard=dashboard, user=current_user)
        cmd = DeleteEmbeddedDashboardCommand(
            dao=cast("AsyncDashboardDAO", dao),
            embedded_dao=cast("AsyncEmbeddedDashboardDAO", embedded_dao),
            dashboard_id=dashboard.id,
        )
        await cmd.execute()
        await event_logger.alog_with_context(
            "dashboard.delete_embedded",
            object_ref=f"dashboard:{id_or_slug}",
        )
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # POST — deep copy
    # ------------------------------------------------------------------
    @post(
        "/{id_or_slug:str}/copy/",
        guards=[require_permission("can_write", "Dashboard")],
        status_code=200,
    )
    async def copy_dashboard(
        self,
        id_or_slug: str,
        data: DashboardCopySchema,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        # Source-dashboard access gate — 1:1 with upstream ``copy_dash``
        # (``@with_dashboard`` → ``DashboardDAO.get_by_id_or_slug`` raises
        # 403/404). The port's plain ``get_by_id_or_slug`` skips it, so without
        # this any ``can_write Dashboard`` user (e.g. Gamma) could COPY — i.e.
        # exfiltrate the full definition of — a dashboard they cannot access.
        from superset.db.filters import dashboard_access_filters

        access_filters = await dashboard_access_filters(security_manager, current_user)
        dashboard = await dao.get_full_by_id_or_slug(
            id_or_slug, extra_filters=access_filters
        )
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", id_or_slug)
        await security_manager.raise_for_access(dashboard=dashboard, user=current_user)
        cmd = CopyDashboardCommand(
            dao=cast("AsyncDashboardDAO", dao),
            dashboard_id=dashboard.id,
            data={
                "dashboard_title": data.dashboard_title,
                "css": data.css,
                "json_metadata": data.json_metadata,
                "duplicate_slices": data.duplicate_slices,
            },
            security_manager=security_manager,
            current_user=current_user,
        )
        new_dash = await cmd.execute()
        await event_logger.alog_with_context(
            "dashboard.copy",
            object_ref=f"dashboard:{id_or_slug}",
            user_id=current_user.id,
        )
        changed_on = getattr(new_dash, "changed_on", None)
        return {
            "result": {
                "id": new_dash.id,
                "last_modified_time": (
                    changed_on.replace(microsecond=0).timestamp()
                    if changed_on
                    else None
                ),
            }
        }

    # ------------------------------------------------------------------
    # Permalink endpoints (merged from DashboardPermalinkController)
    # ------------------------------------------------------------------

    @post(
        "/{pk:int}/permalink",
        guards=[
            deny_anon_with_404,
            require_permission("can_write", "DashboardPermalinkRestApi"),
        ],
        status_code=201,
    )
    async def create_permalink(
        self,
        pk: int,
        data: DashboardPermalinkSchema,
        dao: DashboardDAOProtocol,
        kv_dao: KeyValueDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, str]:
        # Access gate — 1:1 with upstream ``CreateDashboardPermalinkCommand``
        # which resolves the dashboard via ``get_by_id_or_slug`` (403/404). The
        # port's plain ``find_by_id`` skips it, letting a user create a permalink
        # for a dashboard they cannot access.
        from superset.db.filters import dashboard_access_filters

        access_filters = await dashboard_access_filters(security_manager, current_user)
        dashboard = await dao.get_full_by_id_or_slug(
            str(pk), extra_filters=access_filters
        )
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", pk)
        await security_manager.raise_for_access(dashboard=dashboard, user=current_user)
        dashboard_uuid = str(getattr(dashboard, "uuid", "")) or None
        state: dict[str, Any] = {
            "dataMask": data.data_mask,
            "activeTabs": data.active_tabs,
            "anchor": data.anchor,
            "urlParams": data.url_params,
        }
        cmd = CreateDashboardPermalinkCommand(
            dao=cast("AsyncKeyValueDAO", kv_dao),
            dashboard_id=pk,
            state=state,
            dashboard_uuid=dashboard_uuid,
            user_id=current_user.id,
        )
        key = await cmd.execute()
        await event_logger.alog_with_context(
            "dashboard.create_permalink", object_ref=f"dashboard:{pk}"
        )
        # 1:1 with superset_old/dashboards/permalink/api.py:170:
        # ``url_for("Superset.dashboard_permalink", key=key, _external=True)``
        # resolves to ``/superset/dashboard/p/{key}/`` (the SPA route the
        # frontend redirects to / copies). Do NOT hand back the API endpoint.
        return {"key": key, "url": f"/superset/dashboard/p/{key}/"}

    @get(
        "/permalink/{key:str}",
        guards=[require_permission("can_read", "DashboardPermalinkRestApi")],
    )
    async def get_permalink(
        self,
        key: str,
        kv_dao: KeyValueDAOProtocol,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        cmd = GetDashboardPermalinkCommand(
            dao=cast("AsyncKeyValueDAO", kv_dao), key=key
        )
        state = await cmd.execute()

        # 1:1 with superset_old/commands/dashboard/permalink/get.py:47-48 — after
        # the stored value is read, the original re-resolves the dashboard via
        # ``DashboardDAO.get_by_id_or_slug(value["dashboardId"])`` so that a
        # permalink pointing at a deleted dashboard 404s and one pointing at a
        # now-inaccessible dashboard 403s, instead of leaking the saved state.
        dashboard_id = state.get("dashboardId") if isinstance(state, dict) else None
        if dashboard_id is not None:
            from superset.db.filters import dashboard_access_filters

            access_filters = await dashboard_access_filters(
                security_manager, current_user
            )
            dashboard = await dao.get_full_by_id_or_slug(
                str(dashboard_id),
                extra_filters=access_filters,
            )
            if not dashboard:
                raise ObjectNotFoundError("Dashboard", dashboard_id)
            await security_manager.raise_for_access(
                dashboard=dashboard, user=current_user
            )

        await event_logger.alog_with_context(
            "dashboard.get_permalink", object_ref=f"permalink:{key}"
        )
        return state
