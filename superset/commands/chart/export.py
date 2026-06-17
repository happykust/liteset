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
# mypy: ignore-errors
"""Chart export command.

Uses :meth:`superset.models.helpers.ImportExportMixin.export_to_dict` to
build the YAML payload with ``recursive=False, include_defaults=True,
export_uuids=True``.
"""

from __future__ import annotations

import json as _json
import logging
from typing import Any, TYPE_CHECKING

import yaml  # type: ignore[import-untyped]

from superset.exceptions import CommandInvalidError, ObjectNotFoundError
from superset.importexport.export_base import AsyncExportModelsCommand
from superset.models.tags import TagType
from superset.utils.feature_flags import feature_flag_manager
from superset.utils.file import get_filename, secure_filename
from superset.utils.ssh_tunnel import mask_password_info

if TYPE_CHECKING:
    from superset.db.daos.chart import AsyncChartDAO

logger = logging.getLogger(__name__)

EXPORT_VERSION = "1.0.0"

_REMOVE_KEYS = ("datasource_type", "datasource_name", "url_params")


class ExportChartsCommand(AsyncExportModelsCommand):
    """Export charts to a ZIP bundle.

    Uses ``Slice.export_to_dict(recursive=False, include_defaults=True,
    export_uuids=True)``, strips ``datasource_*`` keys, decodes ``params``
    JSON, stamps ``version``, and adds the ``dataset_uuid`` cross-reference.
    Bundles dataset + database YAMLs alongside.
    """

    _resource_type = "Slice"

    def __init__(
        self,
        model_ids: list[int],
        dao: AsyncChartDAO | None = None,
        security_manager: Any = None,
        user: Any = None,
        export_related: bool = True,
    ) -> None:
        super().__init__(model_ids, security_manager=security_manager, user=user)
        self._dao = dao
        self._export_related = export_related

    async def validate(self) -> None:
        from superset.db.filters import chart_access_filters
        from superset.models.slice import Slice

        await self._validate_access(self._dao, Slice, chart_access_filters, "Chart")

    @staticmethod
    def _file_name(model: Any) -> str:
        # Falls back to bare ``<id>`` (not ``unnamed_<id>``) when name is empty.
        slug = secure_filename(model.slice_name or "")
        file_name = f"{slug}_{model.id}" if slug else str(model.id)
        return f"charts/{file_name}.yaml"

    @staticmethod
    def _file_content(model: Any) -> str:
        payload = model.export_to_dict(
            recursive=False,
            include_parent_ref=False,
            include_defaults=True,
            export_uuids=True,
        )
        payload = {k: v for k, v in payload.items() if k not in _REMOVE_KEYS}

        if payload.get("params"):
            try:
                payload["params"] = _json.loads(payload["params"])
            except (TypeError, _json.JSONDecodeError):
                logger.info("Unable to decode `params` field: %s", payload["params"])

        payload["version"] = EXPORT_VERSION
        if getattr(model, "table", None):
            payload["dataset_uuid"] = str(model.table.uuid)

        if feature_flag_manager.is_feature_enabled("TAGGING_SYSTEM"):
            tags = getattr(model, "tags", []) or []
            payload["tags"] = [tag.name for tag in tags if tag.type == TagType.custom]

        return yaml.safe_dump(payload, sort_keys=False)

    async def _export_single(self, model_id: int) -> list[tuple[str, str]]:  # noqa: C901  # complex business logic
        from sqlalchemy import select as sa_select
        from sqlalchemy.orm import selectinload

        if self._dao is None:
            raise CommandInvalidError("DAO not provided for export")

        from superset.models.connectors import SqlaTable
        from superset.models.slice import Slice

        # Eager-load relationships before going async.
        # ``ds.export_to_dict(recursive=True)`` walks ``SqlaTable.export_children``
        # = ["metrics", "columns"] — those collections must be preloaded or the
        # AsyncSession lazy-load fires under no-greenlet and raises
        # ``MissingGreenlet`` while the YAML payload is being assembled.
        stmt = (
            sa_select(Slice)
            .where(Slice.id == model_id)
            .options(
                selectinload(Slice.table).selectinload(SqlaTable.database),
                selectinload(Slice.table).selectinload(SqlaTable.metrics),
                selectinload(Slice.table).selectinload(SqlaTable.columns),
                selectinload(Slice.tags),
            )
        )
        result = await self._dao.session.execute(stmt)
        chart = result.scalars().one_or_none()
        if not chart:
            raise ObjectNotFoundError("Chart", model_id)

        files: list[tuple[str, str]] = [
            (self._file_name(chart), self._file_content(chart))
        ]

        if chart.table:
            ds = chart.table
            ds_payload = ds.export_to_dict(
                recursive=True,
                include_parent_ref=False,
                include_defaults=True,
                export_uuids=True,
            )
            for key in ("params", "template_params", "extra"):
                if ds_payload.get(key):
                    try:
                        ds_payload[key] = _json.loads(ds_payload[key])
                    except (TypeError, _json.JSONDecodeError):
                        pass
            for nested in ("metrics", "columns"):
                for attrs in ds_payload.get(nested, []) or []:
                    if isinstance(attrs.get("extra"), str):
                        try:
                            attrs["extra"] = _json.loads(attrs["extra"])
                        except (TypeError, _json.JSONDecodeError):
                            pass
            ds_payload["version"] = EXPORT_VERSION
            ds_payload["database_uuid"] = (
                str(ds.database.uuid) if getattr(ds, "database", None) else None
            )

            db_slug = (
                get_filename(ds.database.database_name, ds.database.id, skip_id=True)
                if ds.database
                else "unknown"
            )
            ds_slug = get_filename(ds.table_name or "", ds.id)
            files.append(
                (
                    f"datasets/{db_slug}/{ds_slug}.yaml",
                    yaml.safe_dump(ds_payload, sort_keys=False),
                )
            )

            if ds.database:
                db = ds.database
                db_payload = db.export_to_dict(
                    recursive=False,
                    include_parent_ref=False,
                    include_defaults=True,
                    export_uuids=True,
                )
                # Export uses legacy key name; DB stores the renamed column.
                replacements = {"allow_file_upload": "allow_csv_upload"}
                db_payload = {replacements.get(k, k): v for k, v in db_payload.items()}
                if db_payload.get("extra"):
                    import json

                    try:
                        db_payload["extra"] = json.loads(db_payload["extra"])
                    except json.JSONDecodeError:
                        logger.info(
                            "Unable to decode `extra` field: %s", db_payload["extra"]
                        )

                try:
                    from superset.db.daos.database import AsyncSSHTunnelDAO

                    ssh_dao = AsyncSSHTunnelDAO(self._dao.session)
                    ssh_tunnel = await ssh_dao.get_by_database_id(db.id)
                    if ssh_tunnel:
                        ssh_payload = ssh_tunnel.export_to_dict(
                            recursive=False,
                            include_parent_ref=False,
                            include_defaults=True,
                            export_uuids=False,
                        )
                        db_payload["ssh_tunnel"] = mask_password_info(ssh_payload)
                except ImportError:
                    pass

                db_payload["version"] = EXPORT_VERSION
                files.append(
                    (
                        f"databases/{db_slug}.yaml",
                        yaml.safe_dump(db_payload, sort_keys=False),
                    )
                )

        if self._export_related and feature_flag_manager.is_feature_enabled(
            "TAGGING_SYSTEM"
        ):
            chart_tags_list = [
                {"tag_name": tag.name, "description": tag.description}
                for tag in (getattr(chart, "tags", []) or [])
                if "type:" not in tag.name and "owner:" not in tag.name
            ]
            tags_payload: dict[str, Any] = {"tags": chart_tags_list}
            files.append(
                (
                    "tags.yaml",
                    yaml.safe_dump(tags_payload, sort_keys=False),
                )
            )

        return files
