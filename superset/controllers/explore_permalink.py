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

Ported from superset_old/commands/explore/permalink/{create,get}.py and
superset_old/explore/permalink/api.py.  Uses the key_value store with
auto-generated integer ids and a Hashids-encoded short key for URLs.
"""

from __future__ import annotations

import json
from typing import Any

import msgspec
from litestar import Controller, get, post
from litestar.di import Provide
from sqlalchemy.ext.asyncio import AsyncSession

from superset.db.daos.key_value import AsyncKeyValueDAO
from superset.events import event_logger
from superset.exceptions import CommandInvalidError, ObjectNotFoundError
from superset.guards.rbac import require_authentication
from superset.key_value.shared_entries import get_permalink_salt
from superset.key_value.types import KeyValueResource, SharedKey
from superset.key_value.utils import decode_permalink_id, encode_permalink_key
from superset.providers import provide_kv_dao
from superset.typing import KeyValueDAOProtocol, UserProtocol


class ExplorePermalinkCreateSchema(msgspec.Struct, rename="camel"):
    """POST body for explore permalink creation.

    The original Superset endpoint accepts the *state* directly as the
    POST body: ``{formData: {...}, urlParams: [[...], ...]}``.
    The ``chartId``, ``datasourceType``, etc. are derived from
    ``formData`` inside the command, NOT sent as top-level fields.
    """

    form_data: dict[str, Any]
    url_params: list[list[str]] | None = None


class ExplorePermalinkController(Controller):
    path = "/api/v1/explore/permalink"
    tags = ["Explore Permalink"]
    dependencies = {
        "kv_dao": Provide(provide_kv_dao, sync_to_thread=False),
    }

    @post("/", status_code=201, guards=[require_authentication])
    async def create_permalink(
        self,
        data: ExplorePermalinkCreateSchema,
        kv_dao: KeyValueDAOProtocol,
        current_user: UserProtocol,
        session: AsyncSession,
    ) -> dict[str, str]:
        """POST /api/v1/explore/permalink/ — create permalink.

        Matches original CreateExplorePermalinkCommand at
        superset_old/commands/explore/permalink/create.py:56-74:
        1. Derive chart_id / datasource from formData
        2. Store the full state in key_value under EXPLORE_PERMALINK
           resource with auto-generated int id
        3. Encode the int id into a short URL-safe string via
           hashids using a persisted per-install salt
        """
        form_data = data.form_data or {}
        datasource_str = form_data.get("datasource") or ""
        if not datasource_str or "__" not in datasource_str:
            raise CommandInvalidError(
                "formData.datasource is required (format: '<id>__<type>')"
            )

        parts = datasource_str.split("__")
        datasource_id = int(parts[0]) if parts[0].isdigit() else None
        datasource_type = parts[1] if len(parts) >= 2 else "table"

        state = {
            "formData": form_data,
            "urlParams": data.url_params or [],
        }
        payload = {
            "chartId": form_data.get("slice_id"),
            "datasourceId": datasource_id,
            "datasourceType": datasource_type,
            "datasource": datasource_str,
            "state": state,
        }

        # Create entry with auto-generated integer id
        dao = AsyncKeyValueDAO(session)
        entry = await dao.create_entry(
            resource=KeyValueResource.EXPLORE_PERMALINK.value,
            value=json.dumps(payload).encode("utf-8"),
        )
        await session.flush()
        entry_id = entry.id
        if entry_id is None:
            raise CommandInvalidError("Unexpected missing key id")

        # Encode the int id into a short hashids string using a
        # per-install salt (persisted in the app resource).
        salt = await get_permalink_salt(session, SharedKey.EXPLORE_PERMALINK_SALT)
        key = encode_permalink_key(key=entry_id, salt=salt)

        event_logger.log("explore_permalink.create", user_id=current_user.id)
        return {"key": key, "url": f"/explore/p/{key}/"}

    @get("/{key:str}", guards=[require_authentication])
    async def get_permalink(
        self,
        key: str,
        kv_dao: KeyValueDAOProtocol,
        session: AsyncSession,
    ) -> dict[str, Any]:
        """GET /api/v1/explore/permalink/{key} — resolve permalink.

        Decodes the hashids key back to an int id, looks up the
        key_value entry, and returns the stored payload.

        The original Flask endpoint spreads the stored fields
        directly into the response (``**value``), so the frontend
        receives ``{chartId, datasourceId, datasource, state, ...}``.
        """
        salt = await get_permalink_salt(session, SharedKey.EXPLORE_PERMALINK_SALT)
        try:
            entry_id = decode_permalink_id(key, salt=salt)
        except Exception as ex:  # noqa: BLE001
            raise ObjectNotFoundError("ExplorePermalink", key) from ex

        dao = AsyncKeyValueDAO(session)
        entry = await dao.get_entry_by_key(
            resource=KeyValueResource.EXPLORE_PERMALINK.value,
            key=entry_id,
        )
        if entry is None:
            raise ObjectNotFoundError("ExplorePermalink", key)

        try:
            payload = json.loads(entry.value.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = {}
        if isinstance(payload, dict):
            return payload
        return {"value": payload}
