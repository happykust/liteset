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
"""Explore form data controller — temporary cache for explore form state.

The frontend sends ``{datasource_id, datasource_type, form_data, chart_id?}``
with ``tab_id`` as a query parameter.  The original Superset stores the
serialized value in a KV table keyed by a UUID.
"""

from __future__ import annotations

import logging
from typing import Any, Literal
from uuid import uuid4

import msgspec
from litestar import Controller, delete, get, post, put
from litestar.connection import Request
from litestar.di import Provide
from litestar.params import Parameter

from superset.commands.explore_form_data.utils import check_access
from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError
from superset.guards.rbac import require_permission
from superset.providers import (
    provide_chart_dao,
    provide_dataset_dao,
    provide_query_dao,
)
from superset.typing import (
    ChartDAOProtocol,
    DatasetDAOProtocol,
    QueryDAOProtocol,
    SecurityManagerProtocol,
    UserProtocol,
)

logger = logging.getLogger(__name__)

def _form_data_cache() -> Any:
    """The ``cache_manager.explore_form_data_cache`` slot.

    Honours ``EXPLORE_FORM_DATA_CACHE_CONFIG["CACHE_TYPE"]`` (metastore by
    default, Redis when configured) and owns the TTL
    (``CACHE_DEFAULT_TIMEOUT``).  Entry keys are the raw upstream key
    strings — the metastore backend hashes them to uuid3 under the
    ``superset_metastore_cache`` resource, so entries written by an
    upstream Superset instance keep resolving after a migration.
    """
    from superset.extensions import cache_manager

    return cache_manager.explore_form_data_cache


def _contextual_key_str(
    session_id: str,
    tab_id: int | None,
    datasource_id: int,
    chart_id: int | None,
    datasource_type: str,
) -> str:
    """Build the contextual key string for tab-aware form_data reuse.

    1:1 with ``superset_old/temporary_cache/utils.py:cache_key()`` which
    joins args with ``";"`` plus
    ``superset_old/commands/explore/form_data/create.py:49-51``:
    ``cache_key(session.get("_id"), tab_id, datasource_id, chart_id, datasource_type)``.
    """
    return ";".join(
        str(a) for a in [session_id, tab_id, datasource_id, chart_id, datasource_type]
    )


DatasourceType = Literal["table", "dataset", "query", "saved_query", "view"]


def _validate_form_data_json(form_data: str | None) -> None:
    """Validate that ``form_data`` is well-formed JSON.

    1:1 with the ``validate()`` hooks of the original
    ``CreateFormDataCommand`` / ``UpdateFormDataCommand``
    (``superset_old/commands/explore/form_data/{create,update}.py``):
    ``validate_json(form_data)`` is run whenever ``form_data`` is truthy, and a
    failure surfaces as a marshmallow ``ValidationError`` → HTTP 400. Here we
    use ``superset.utils.json.validate_json`` (which raises ``JSONDecodeError``)
    and re-raise it as a 400 with the original "JSON not valid" message.
    """
    from superset.exceptions import SupersetGenericErrorException
    from superset.utils.json import JSONDecodeError, validate_json

    if form_data:
        try:
            validate_json(form_data)
        except JSONDecodeError as ex:
            raise SupersetGenericErrorException("JSON not valid", status=400) from ex


class FormDataPostSchema(msgspec.Struct):
    """POST body matching the original Superset explore form_data API."""

    datasource_id: int
    datasource_type: DatasourceType
    form_data: str
    chart_id: int | None = None


class FormDataPutSchema(msgspec.Struct):
    """PUT body matching the original Superset explore form_data API."""

    datasource_id: int
    datasource_type: DatasourceType
    form_data: str
    chart_id: int | None = None


class ExploreFormDataController(Controller):
    path = "/api/v1/explore/form_data"
    tags = ["Explore Form Data"]
    resource = "explore_form_data"
    dependencies = {
        "chart_dao": Provide(provide_chart_dao, sync_to_thread=False),
        "dataset_dao": Provide(provide_dataset_dao, sync_to_thread=False),
        "query_dao": Provide(provide_query_dao, sync_to_thread=False),
    }

    @get(
        "/{key:str}",
        guards=[require_permission("can_read", "ExploreFormDataRestApi")],
    )
    async def get_value(
        self,
        key: str,
        chart_dao: ChartDAOProtocol,
        dataset_dao: DatasetDAOProtocol,
        query_dao: QueryDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """GET /{key} — retrieve cached form_data.

        1:1 with original GetFormDataCommand (get.py:42-60):
        - Reads stored envelope.
        - Calls check_access(datasource_id, chart_id, datasource_type).
        - When EXPLORE_FORM_DATA_CACHE_CONFIG["REFRESH_TIMEOUT_ON_RETRIEVAL"]
          is True (default), refreshes the entry TTL on every successful read
          (``cache_manager.explore_form_data_cache.set(key, state)``).
        """
        cache = _form_data_cache()
        entry = await cache.get(key)
        if entry is None:
            raise ObjectNotFoundError(self.resource, key)

        if isinstance(entry, dict):
            datasource_id: int = entry.get("datasource_id") or 0
            datasource_type: str = entry.get("datasource_type") or "table"
            chart_id: int | None = entry.get("chart_id")
            # 1:1 with superset_old/commands/explore/form_data/get.py:48-53 —
            # ``check_access`` is invoked unconditionally whenever state exists.
            # The original lets ``check_datasource_access`` itself reject a falsy
            # ``datasource_id`` (``DatasourceNotFoundValidationError``); do not
            # short-circuit on ``datasource_id == 0`` here.
            await check_access(
                datasource_id=datasource_id,
                chart_id=chart_id,
                datasource_type=datasource_type,
                dataset_dao=dataset_dao,
                query_dao=query_dao,
                chart_dao=chart_dao,
                security_manager=security_manager,
                user=current_user,
            )
            # REFRESH_TIMEOUT_ON_RETRIEVAL (default True) — 1:1 with
            # get.py:54-55: ``if self._refresh_timeout:
            #     cache_manager.explore_form_data_cache.set(key, state)``
            # which re-stores the value with a fresh full-TTL expires_on.
            _refresh = True
            try:
                from superset.config import settings as _settings

                _refresh = bool(
                    _settings.explore_form_data_cache_config.get(
                        "REFRESH_TIMEOUT_ON_RETRIEVAL", True
                    )
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "Failed to read REFRESH_TIMEOUT_ON_RETRIEVAL", exc_info=True
                )
            if _refresh:
                # Slot ``set`` stamps a fresh full-TTL window.
                await cache.set(key, entry)
            if "form_data" in entry:
                return {"form_data": entry["form_data"]}

        return {"form_data": entry}

    @post(
        "/",
        status_code=201,
        # 1:1 with superset_old/explore/form_data/api.py:50-51 — ``@protect()``
        # returns 401 (not 403) for anonymous callers. ``require_permission``
        # raises ``NotAuthorizedException`` (401) for unauthenticated users that
        # lack the Public-role permission, so we drop the ``deny_anon_with_403``
        # guard which would otherwise 403 anonymous POSTs.
        guards=[
            require_permission("can_write", "ExploreFormDataRestApi"),
        ],
    )
    async def create_value(
        self,
        request: Request[Any, Any, Any],
        data: FormDataPostSchema,
        chart_dao: ChartDAOProtocol,
        dataset_dao: DatasetDAOProtocol,
        query_dao: QueryDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        tab_id: int | None = Parameter(query="tab_id", default=None, required=False),
    ) -> dict[str, str]:
        """POST / — create new cached form_data.

        1:1 with original CreateFormDataCommand (create.py:40-68):
        - Validates that ``form_data`` is valid JSON.
        - Calls check_access before storing.
        - Reuses the existing key when the same session+tab+datasource+chart
          context is re-saved with a truthy ``tab_id`` (contextual key reuse).
        - Stores with a 7-day TTL matching EXPLORE_FORM_DATA_CACHE_CONFIG
          CACHE_DEFAULT_TIMEOUT (original uses SupersetMetastoreCache which
          converts it to ``expires_on`` in KeyValueDAO.upsert_entry).
        """
        _validate_form_data_json(data.form_data)
        await check_access(
            datasource_id=data.datasource_id,
            chart_id=data.chart_id,
            datasource_type=data.datasource_type,
            dataset_dao=dataset_dao,
            query_dao=query_dao,
            chart_dao=chart_dao,
            security_manager=security_manager,
            user=current_user,
        )

        # --- Contextual key reuse (1:1 with create.py:49-54) ---
        # ``session.get("_id")`` → session cookie value in the ASGI context.
        # The Flask session ``_id`` is the raw itsdangerous cookie value;
        # the port's equivalent is the ``session`` cookie.
        session_id: str = request.cookies.get("session", "")
        ctx_str = _contextual_key_str(
            session_id,
            tab_id,
            data.datasource_id,
            data.chart_id,
            data.datasource_type,
        )
        cache = _form_data_cache()
        # Try to reuse an existing key for the same tab context.
        # 1:1 with ``key = cache_manager.explore_form_data_cache.get(contextual_key)``
        # and the guard ``if not key or not tab_id: key = random_key()``.
        existing_key = await cache.get(ctx_str)
        # ``tab_id is not None`` — the original reads request.args.get("tab_id")
        # as a STRING, so "0" is truthy; an int 0 here must also reuse the key.
        if existing_key and isinstance(existing_key, str) and tab_id is not None:
            key = existing_key
        else:
            key = str(uuid4())

        # 1:1 with create.py:55 — only persist the envelope + contextual
        # mapping when ``form_data`` is truthy; when empty the original
        # returns the key without any cache write.
        if data.form_data:
            state = {
                "owner": current_user.id,
                "datasource_id": data.datasource_id,
                "datasource_type": data.datasource_type,
                "chart_id": data.chart_id,
                "form_data": data.form_data,
            }
            # 1:1 with ``cache_manager.explore_form_data_cache.set(key, state)``
            # then ``.set(contextual_key, key)`` — the slot stamps the TTL.
            await cache.set(key, state)
            await cache.set(ctx_str, key)
        await event_logger.alog_with_context(
            "explore_form_data.create",
            user_id=current_user.id,
        )
        return {"key": key}

    @put(
        "/{key:str}",
        guards=[require_permission("can_write", "ExploreFormDataRestApi")],
    )
    async def update_value(
        self,
        request: Request[Any, Any, Any],
        key: str,
        data: FormDataPutSchema,
        chart_dao: ChartDAOProtocol,
        dataset_dao: DatasetDAOProtocol,
        query_dao: QueryDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        tab_id: int | None = Parameter(query="tab_id", default=None, required=False),
    ) -> dict[str, str]:
        """PUT /{key} — update cached form_data.

        1:1 with original UpdateFormDataCommand (update.py:48-86):
        - Validates form_data JSON.
        - Calls check_access for the new datasource/chart.
        - Checks ownership before allowing the update.
        - Generates a new key if tab_id changes or is falsy (1:1 with
          ``if not key or not tab_id: key = random_key()``).
        - Stores contextual → key mapping with same TTL.
        - Stores form_data with 7-day TTL.
        """
        from litestar.exceptions import PermissionDeniedException

        _validate_form_data_json(data.form_data)

        cache = _form_data_cache()
        entry = await cache.get(key)
        # When the cache entry is absent (cache miss / expired), the original
        # UpdateFormDataCommand.run() falls through the ``if state and form_data``
        # guard and returns the original key unchanged → HTTP 200 (not 404).
        # Do NOT raise here; let the guard at ``if entry and data.form_data`` below
        # handle the miss path identically to the original.

        # Datasource + chart access check — mirrors UpdateFormDataCommand.
        await check_access(
            datasource_id=data.datasource_id,
            chart_id=data.chart_id,
            datasource_type=data.datasource_type,
            dataset_dao=dataset_dao,
            query_dao=query_dao,
            chart_dao=chart_dao,
            security_manager=security_manager,
            user=current_user,
        )

        # 1:1 with update.py:61 ``if state and form_data:`` — when the state
        # exists but form_data is falsy (empty string), the original returns
        # the original key unchanged without modifying the cache.
        # ``isinstance(entry, dict)`` matches the GET/DELETE guards: a
        # corrupted non-dict JSON value must not 500 on ``entry.get(...)``.
        if entry and isinstance(entry, dict) and data.form_data:
            # Owner check — 1:1 with update.py:62:
            #   ``if state["owner"] != owner: raise TemporaryCacheAccessDeniedError()``
            # The original is unconditional: None (anon-owned entry) != any
            # authenticated user_id → raises.  Do NOT guard on ``owner is not
            # None``; that would silently skip the check for None-owner entries.
            owner = entry.get("owner")
            if owner != current_user.id:
                raise PermissionDeniedException(
                    detail="You don't have access to this resource"
                )

            # --- Contextual key reuse / rotation (1:1 with update.py:65-73) ---
            # Generate a new key if tab_id is falsy or the contextual key lookup
            # returns nothing.  When a new key is minted, the contextual mapping
            # is stored so subsequent saves for the same tab reuse the new key.
            session_id: str = request.cookies.get("session", "")
            ctx_str = _contextual_key_str(
                session_id,
                tab_id,
                data.datasource_id,
                data.chart_id,
                data.datasource_type,
            )
            existing_ctx_key = await cache.get(ctx_str)
            # As in create: "0" is a truthy tab id in the original.
            if (
                existing_ctx_key
                and isinstance(existing_ctx_key, str)
                and tab_id is not None
            ):
                # Reuse the contextual key's mapped UUID.
                key = existing_ctx_key
            else:
                # Mint a fresh UUID and persist the contextual mapping.
                key = str(uuid4())
                # 1:1 with update.py:73 — only store the contextual mapping
                # when a new key is minted.
                await cache.set(ctx_str, key)

            new_state = {
                "owner": current_user.id,
                "datasource_id": data.datasource_id,
                "datasource_type": data.datasource_type,
                "chart_id": data.chart_id,
                "form_data": data.form_data,
            }
            # Store form_data under the (possibly rotated) UUID key — the
            # slot stamps the TTL.  1:1 with
            # ``cache_manager.explore_form_data_cache.set(key, new_state)``.
            await cache.set(key, new_state)
        await event_logger.alog_with_context(
            "explore_form_data.update",
            user_id=current_user.id,
        )
        return {"key": key}

    @delete(
        "/{key:str}",
        status_code=200,
        guards=[require_permission("can_write", "ExploreFormDataRestApi")],
    )
    async def delete_value(
        self,
        request: Request[Any, Any, Any],
        key: str,
        chart_dao: ChartDAOProtocol,
        dataset_dao: DatasetDAOProtocol,
        query_dao: QueryDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, str]:
        """DELETE /{key} — delete cached form_data.

        1:1 with original DeleteFormDataCommand (delete.py:43-65):
        - Reads stored envelope to extract datasource/chart metadata.
        - Calls check_access.
        - Verifies ownership.
        - Deletes both the contextual-key mapping and the form_data entry
          (``cache_manager.explore_form_data_cache.delete(contextual_key)``
          then ``.delete(key)``).
        """
        from litestar.exceptions import PermissionDeniedException

        cache = _form_data_cache()
        entry = await cache.get(key)
        if entry is None:
            raise ObjectNotFoundError(self.resource, key)

        if isinstance(entry, dict):
            datasource_id: int = entry.get("datasource_id") or 0
            datasource_type: str = entry.get("datasource_type") or "table"
            chart_id: int | None = entry.get("chart_id")
            # 1:1 with superset_old/commands/explore/form_data/delete.py:49-53 —
            # ``check_access`` runs unconditionally when state exists; the falsy
            # ``datasource_id`` case is handled inside ``check_datasource_access``.
            await check_access(
                datasource_id=datasource_id,
                chart_id=chart_id,
                datasource_type=datasource_type,
                dataset_dao=dataset_dao,
                query_dao=query_dao,
                chart_dao=chart_dao,
                security_manager=security_manager,
                user=current_user,
            )
            # Owner check — 1:1 with delete.py:54:
            #   ``if state["owner"] != get_user_id():``
            #       ``raise TemporaryCacheAccessDeniedError()``
            # The original is unconditional: None (anon-owned entry) != any
            # authenticated user_id → raises.  Do NOT guard on
            # ``owner is not None``; that skips the check for None-owner
            # entries, allowing any caller to delete them.
            owner = entry.get("owner")
            if owner != current_user.id:
                raise PermissionDeniedException(
                    detail="You don't have access to this resource"
                )
            # Delete the contextual-key → form_data-key mapping (1:1 with
            # delete.py:56-60:
            #   ``tab_id = self._cmd_params.tab_id``
            #   ``contextual_key = cache_key(session.get("_id"), tab_id, ...)``
            #   ``cache_manager.explore_form_data_cache.delete(contextual_key)``
            # The original DELETE API always constructs ``CommandParameters(key=key)``
            # without tab_id, so ``tab_id`` is ``None`` here — matching the
            # original's ``cache_key(session_id, None, ds_id, chart_id, ds_type)``.
            session_id: str = request.cookies.get("session", "")
            ctx_str = _contextual_key_str(
                session_id,
                None,  # 1:1: original delete uses tab_id=None (no tab_id param)
                datasource_id,
                chart_id,
                datasource_type,
            )
            # Best-effort: a missing contextual entry is simply a no-op.
            await cache.delete(ctx_str)

        # The entry existed above — delete it.  1:1 with
        # ``cache_manager.explore_form_data_cache.delete(key)``.
        await cache.delete(key)
        await event_logger.alog_with_context(
            "explore_form_data.delete",
            user_id=current_user.id,
        )
        # 1:1 with upstream ``response(200, message="Deleted successfully")``.
        return {"message": "Deleted successfully"}
