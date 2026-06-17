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
"""Explore permalink controller — create and resolve explore permalinks.

Uses the key_value store with auto-generated integer ids and a Hashids-encoded
short key for URLs.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import msgspec
from litestar import Controller, get, post, Request
from litestar.di import Provide
from sqlalchemy.ext.asyncio import AsyncSession

from superset.commands.explore_permalink.utils import check_access as check_chart_access
from superset.db.daos.key_value import AsyncKeyValueDAO
from superset.events import event_logger
from superset.exceptions import CommandInvalidError, ObjectNotFoundError
from superset.guards.rbac import require_permission
from superset.key_value.shared_entries import get_permalink_salt
from superset.key_value.types import KeyValueResource, SharedKey
from superset.key_value.utils import decode_permalink_id, encode_permalink_key
from superset.providers import (
    provide_chart_dao,
    provide_dataset_dao,
    provide_kv_dao,
    provide_query_dao,
)
from superset.typing import (
    ChartDAOProtocol,
    DatasetDAOProtocol,
    KeyValueDAOProtocol,
    QueryDAOProtocol,
    SecurityManagerProtocol,
    UserProtocol,
)
from superset.utils.core import DatasourceType


class ExplorePermalinkCreateSchema(msgspec.Struct, rename="camel"):
    """POST body for explore permalink creation.

    The original Superset endpoint accepts the *state* directly as the
    POST body: ``{formData: {...}, urlParams: [[...], ...]}``.
    The ``chartId``, ``datasourceType``, etc. are derived from
    ``formData`` inside the command, NOT sent as top-level fields.
    """

    form_data: dict[str, Any]
    # ``urlParams`` is ``allow_none=True`` upstream: an EXPLICIT null is kept
    # in the loaded dict (and thus in the stored state), while an absent field
    # is omitted — hence UNSET, not None, as the absent marker.
    # Each item is a 2-element ``(key, value)`` pair; a list of any other
    # length is rejected by the schema.
    url_params: list[tuple[str | None, str | None]] | None | msgspec.UnsetType = (
        msgspec.UNSET
    )


class ExplorePermalinkController(Controller):
    path = "/api/v1/explore/permalink"
    tags = ["Explore Permalink"]
    dependencies = {
        "kv_dao": Provide(provide_kv_dao, sync_to_thread=False),
        "chart_dao": Provide(provide_chart_dao, sync_to_thread=False),
        "dataset_dao": Provide(provide_dataset_dao, sync_to_thread=False),
        "query_dao": Provide(provide_query_dao, sync_to_thread=False),
    }

    @post(
        "/",
        status_code=201,
        guards=[require_permission("can_write", "ExplorePermalinkRestApi")],
    )
    async def create_permalink(
        self,
        request: Request[Any, Any, Any],
        data: ExplorePermalinkCreateSchema,
        kv_dao: KeyValueDAOProtocol,
        chart_dao: ChartDAOProtocol,
        dataset_dao: DatasetDAOProtocol,
        query_dao: QueryDAOProtocol,
        current_user: UserProtocol,
        security_manager: SecurityManagerProtocol,
        session: AsyncSession,
    ) -> dict[str, str]:
        """POST /api/v1/explore/permalink/ — create permalink.

        1. Derive chart_id / datasource from formData
        2. Call check_chart_access (datasource + optional chart ownership check)
        3. Store the full state in key_value under EXPLORE_PERMALINK resource
           with an auto-generated int id
        4. Encode the int id into a short URL-safe string via hashids using a
           persisted per-install salt
        """
        form_data = data.form_data or {}
        # Parse ``formData.datasource`` (``"<id>__<type>"`` format).
        # ``check_chart_access`` is invoked unconditionally with the parsed integer
        # id — a non-numeric datasource id must NOT bypass access control.
        datasource_str = form_data.get("datasource") or ""
        if not datasource_str or "__" not in datasource_str:
            raise CommandInvalidError(
                "formData.datasource is required (format: '<id>__<type>')"
            )

        d_id, d_type = datasource_str.split("__")
        datasource_id = int(d_id)
        # Validate datasource type via DatasourceType enum.
        # ValueError for unknown types propagates as 500 (no try/except).
        datasource_type = DatasourceType(d_type).value
        chart_id: int | None = form_data.get("slice_id")

        # check_chart_access before storing the permalink.
        await check_chart_access(
            datasource_id=datasource_id,
            chart_id=chart_id,
            datasource_type=datasource_type,
            dataset_dao=dataset_dao,
            query_dao=query_dao,
            chart_dao=chart_dao,
            security_manager=security_manager,
            user=current_user,
        )

        # ``urlParams`` is only included when provided — but an EXPLICIT
        # ``urlParams: null`` IS provided (allow_none=True) and must be stored.
        state: dict[str, Any] = {"formData": form_data}
        if data.url_params is not msgspec.UNSET:
            state["urlParams"] = data.url_params
        payload = {
            "chartId": chart_id,
            "datasourceId": datasource_id,
            "datasourceType": datasource_type,
            "datasource": datasource_str,
            "state": state,
        }

        # Create entry with auto-generated integer id; thread the current user id
        # into ``created_by_fk`` via KeyValueDAO.create_entry.
        dao = AsyncKeyValueDAO(session)
        entry = await dao.create_entry(
            resource=KeyValueResource.EXPLORE_PERMALINK.value,
            value=json.dumps(payload).encode("utf-8"),
            user_id=current_user.id,
        )
        await session.flush()
        entry_id = entry.id
        if entry_id is None:
            raise CommandInvalidError("Unexpected missing key id")

        # Encode the int id into a short hashids string using a
        # per-install salt (persisted in the app resource).
        salt = await get_permalink_salt(session, SharedKey.EXPLORE_PERMALINK_SALT)
        key = encode_permalink_key(key=int(entry_id), salt=salt)

        await event_logger.alog_with_context(
            "explore_permalink.create", user_id=current_user.id
        )
        base_url = str(request.base_url).rstrip("/")
        return {"key": key, "url": f"{base_url}/superset/explore/p/{key}/"}

    @get(
        "/{key:str}",
        guards=[require_permission("can_read", "ExplorePermalinkRestApi")],
    )
    async def get_permalink(
        self,
        key: str,
        kv_dao: KeyValueDAOProtocol,
        chart_dao: ChartDAOProtocol,
        dataset_dao: DatasetDAOProtocol,
        query_dao: QueryDAOProtocol,
        current_user: UserProtocol,
        security_manager: SecurityManagerProtocol,
        session: AsyncSession,
    ) -> dict[str, Any]:
        """GET /api/v1/explore/permalink/{key} — resolve permalink.

        Decodes the hashids key back to an int id, looks up the key_value entry,
        re-validates datasource access, and returns the stored payload.

        The stored fields are spread directly into the response so the frontend
        receives ``{chartId, datasourceId, datasource, state, ...}``.
        """
        # decode_permalink_id raises KeyValueParseKeyError for an invalid key.
        # Must NOT convert this to a 404 — let it propagate as HTTP 500.
        salt = await get_permalink_salt(session, SharedKey.EXPLORE_PERMALINK_SALT)
        entry_id = decode_permalink_id(key, salt=salt)

        dao = AsyncKeyValueDAO(session)
        entry = await dao.get_entry_by_key(
            resource=KeyValueResource.EXPLORE_PERMALINK.value,
            key=entry_id,
        )
        # Expired entries must be treated as non-existent (→ 404).
        if entry is None or (
            entry.expires_on is not None and entry.expires_on <= datetime.now()
        ):
            raise ObjectNotFoundError("ExplorePermalink", key)

        # No try/except: a corrupted stored value raises json.JSONDecodeError,
        # which propagates as HTTP 500 (NOT silently coerced to ``{}``).
        payload = json.loads(entry.value.decode("utf-8"))

        # Every permalink read emits an audit log entry.
        await event_logger.alog_with_context(
            "explore_permalink.get", user_id=getattr(current_user, "id", None)
        )

        # Re-validate datasource access for every GET so permission revocations
        # are honoured.
        if isinstance(payload, dict):
            # Support both camelCase and snake_case payload conventions.
            datasource_id: int = (
                payload.get("datasourceId")
                or payload.get("datasource_id")
                or payload.get("datasetId")
                or payload.get("dataset_id")
                or 0
            )
            chart_id: int | None = payload.get("chartId") or payload.get("chart_id")
            datasource_type: str = (
                payload.get("datasourceType")
                or payload.get("datasource_type")
                or "table"
            )
            # ``check_chart_access`` is invoked unconditionally whenever a stored
            # value exists. The falsy ``datasource_id == 0`` case is rejected
            # inside ``check_datasource_access`` rather than silently skipped.
            from superset.exceptions import DatasetNotFoundError, SupersetException

            try:
                await check_chart_access(
                    datasource_id=datasource_id,
                    chart_id=chart_id,
                    datasource_type=datasource_type,
                    dataset_dao=dataset_dao,
                    query_dao=query_dao,
                    chart_dao=chart_dao,
                    security_manager=security_manager,
                    user=current_user,
                )
            except (ObjectNotFoundError, DatasetNotFoundError) as ex:
                # DatasetNotFoundError → HTTP 500 (wraps into SupersetException
                # to match the original handler which does not catch it).
                # A missing CHART raises ChartNotFoundError (not caught here) →
                # propagates as 404.
                raise SupersetException(message=getattr(ex, "message", str(ex))) from ex
            return payload
        return {"value": payload}
