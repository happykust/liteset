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

import json
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid3, uuid4

import msgspec
from litestar import Controller, delete, get, post, put
from litestar.connection import Request
from litestar.di import Provide
from litestar.params import Parameter

from superset.commands.explore_form_data.utils import check_access
from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError
from superset.guards.rbac import require_permission
from superset.key_value.utils import get_uuid_namespace
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

# Namespace UUID for derive contextual-key UUIDs (one per explore session+tab).
# Using "explore_form_data_contextual" matches the approach of
# ``SupersetMetastoreCache.get_key()``, which does
# ``uuid3(namespace_of_cache_prefix, key_string)`` so contextual keys and
# value keys live in the same table but under deterministically different UUIDs.
_FORM_DATA_CONTEXTUAL_NS: UUID = get_uuid_namespace("explore_form_data_contextual")


def _form_data_expires_on() -> datetime:
    """Return the expiry datetime for a new form_data entry.

    1:1 with ``SupersetMetastoreCache._get_expiry()`` called from
    ``SupersetMetastoreCache.set()`` via
    ``EXPLORE_FORM_DATA_CACHE_CONFIG["CACHE_DEFAULT_TIMEOUT"] = timedelta(days=7)``.
    We read ``settings.explore_form_data_cache_config`` so a user-supplied
    override (via ``EXPLORE_FORM_DATA_CACHE_CONFIG`` in their
    ``superset_config.py``) is honoured at runtime.
    """
    try:
        from superset.config import settings as _settings

        timeout: int = (
            _settings.explore_form_data_cache_config.get(
                "CACHE_DEFAULT_TIMEOUT", 604800
            )
        )
    except Exception:  # noqa: BLE001
        timeout = 604800  # 7 days fallback
    return datetime.now() + timedelta(seconds=timeout)


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


def _contextual_uuid(ctx_key: str) -> str:
    """Derive a deterministic UUID string from a contextual key string.

    The original stores the contextual→value-key mapping in the *same*
    ``SupersetMetastoreCache`` instance used for the value entries, but the
    cache's ``get_key()`` converts string keys to ``uuid3(namespace, key)``
    (``superset_old/extensions/metastore_cache.py:71-72``).  We replicate
    that transform so contextual UUIDs are stable across restarts and cannot
    collide with random value-key UUIDs.
    """
    return str(uuid3(_FORM_DATA_CONTEXTUAL_NS, ctx_key))

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
            raise SupersetGenericErrorException(
                "JSON not valid", status=400
            ) from ex


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
        "kv_dao": Provide(provide_kv_dao, sync_to_thread=False),
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
        kv_dao: KeyValueDAOProtocol,
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
        raw = await kv_dao.get_value(
            resource=self.resource,
            resource_id=0,
            key=key,
        )
        if raw is None:
            raise ObjectNotFoundError(self.resource, key)

        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            entry = {}

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
                pass
            if _refresh:
                await kv_dao.set_value(
                    resource=self.resource,
                    resource_id=0,
                    key=key,
                    value=raw,
                    expires_on=_form_data_expires_on(),  # type: ignore[call-arg]
                )
            if "value" in entry:
                return {"form_data": entry["value"]}

        return {"form_data": raw}

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
        kv_dao: KeyValueDAOProtocol,
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
        ctx_uuid = _contextual_uuid(ctx_str)
        # Try to reuse an existing key for the same tab context.
        # 1:1 with ``key = cache_manager.explore_form_data_cache.get(contextual_key)``
        # and the guard ``if not key or not tab_id: key = random_key()``.
        existing_key_raw = await kv_dao.get_value(
            resource=self.resource,
            resource_id=0,
            key=ctx_uuid,
        )
        if existing_key_raw and tab_id:
            # Reuse the stored form_data UUID key.
            key = existing_key_raw.strip()
        else:
            key = str(uuid4())

        expires_on = _form_data_expires_on()
        envelope = json.dumps(
            {
                "owner": current_user.id,
                "datasource_id": data.datasource_id,
                "datasource_type": data.datasource_type,
                "chart_id": data.chart_id,
                "tab_id": tab_id,
                "value": data.form_data,
            }
        )
        # Store form_data under the (possibly reused) UUID key with TTL.
        # 1:1 with ``cache_manager.explore_form_data_cache.set(key, state)``.
        await kv_dao.set_value(
            resource=self.resource,
            resource_id=0,
            key=key,
            value=envelope,
            user_id=current_user.id,  # type: ignore[call-arg]
            expires_on=expires_on,  # type: ignore[call-arg]
        )
        # Store/refresh the contextual → form_data key mapping with same TTL.
        # 1:1 with ``cache_manager.explore_form_data_cache.set(contextual_key, key)``.
        await kv_dao.set_value(
            resource=self.resource,
            resource_id=0,
            key=ctx_uuid,
            value=key,
            user_id=current_user.id,  # type: ignore[call-arg]
            expires_on=expires_on,  # type: ignore[call-arg]
        )
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
        kv_dao: KeyValueDAOProtocol,
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

        existing = await kv_dao.get_value(
            resource=self.resource,
            resource_id=0,
            key=key,
        )
        if existing is None:
            raise ObjectNotFoundError(self.resource, key)

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

        # Owner check — original raises TemporaryCacheAccessDeniedError
        # when state["owner"] != get_user_id().
        try:
            entry = json.loads(existing)
        except (json.JSONDecodeError, TypeError):
            entry = {}
        owner = entry.get("owner")
        if owner is not None and owner != current_user.id:
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
        ctx_uuid = _contextual_uuid(ctx_str)
        existing_ctx_key = await kv_dao.get_value(
            resource=self.resource,
            resource_id=0,
            key=ctx_uuid,
        )
        if existing_ctx_key and tab_id:
            # Reuse the contextual key's mapped UUID.
            key = existing_ctx_key.strip()
        else:
            # Mint a fresh UUID and persist the contextual mapping.
            key = str(uuid4())

        expires_on = _form_data_expires_on()
        envelope = json.dumps(
            {
                "owner": current_user.id,
                "datasource_id": data.datasource_id,
                "datasource_type": data.datasource_type,
                "chart_id": data.chart_id,
                "tab_id": tab_id,
                "value": data.form_data,
            }
        )
        # Store form_data under the (possibly rotated) UUID key with TTL.
        # 1:1 with ``cache_manager.explore_form_data_cache.set(key, new_state)``.
        await kv_dao.set_value(
            resource=self.resource,
            resource_id=0,
            key=key,
            value=envelope,
            user_id=current_user.id,  # type: ignore[call-arg]
            expires_on=expires_on,  # type: ignore[call-arg]
        )
        # Store/refresh the contextual → form_data key mapping with same TTL.
        # 1:1 with ``cache_manager.explore_form_data_cache.set(contextual_key, key)``
        # (only on the new-key branch in the original, but refreshing TTL on
        # every write is harmless and keeps the contextual entry alive).
        await kv_dao.set_value(
            resource=self.resource,
            resource_id=0,
            key=ctx_uuid,
            value=key,
            user_id=current_user.id,  # type: ignore[call-arg]
            expires_on=expires_on,  # type: ignore[call-arg]
        )
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
        kv_dao: KeyValueDAOProtocol,
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

        raw = await kv_dao.get_value(
            resource=self.resource,
            resource_id=0,
            key=key,
        )
        if raw is None:
            raise ObjectNotFoundError(self.resource, key)

        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            entry = {}

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
            # Owner check — original raises TemporaryCacheAccessDeniedError
            # when state["owner"] != get_user_id().
            owner = entry.get("owner")
            if owner is not None and owner != current_user.id:
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
            ctx_uuid = _contextual_uuid(ctx_str)
            # Best-effort: ignore errors if the contextual entry is already gone.
            await kv_dao.delete_value(
                resource=self.resource,
                resource_id=0,
                key=ctx_uuid,
            )

        deleted = await kv_dao.delete_value(
            resource=self.resource,
            resource_id=0,
            key=key,
        )
        if not deleted:
            raise ObjectNotFoundError(self.resource, key)
        await event_logger.alog_with_context(
            "explore_form_data.delete",
            user_id=current_user.id,
        )
        # 1:1 with upstream ``response(200, message="Deleted successfully")``.
        return {"message": "Deleted successfully"}
