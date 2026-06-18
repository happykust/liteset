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
"""OAuth2 helpers.

Provides:

* :class:`OAuth2ClientConfig`, :class:`OAuth2State`, :class:`OAuth2TokenResponse`
  TypedDicts.
* :func:`encode_oauth2_state` / :func:`decode_oauth2_state` — JWT-backed
  signed-token round-trip for the OAuth2 ``state`` parameter.
* :func:`get_oauth2_access_token` — returns a valid access token for the
  ``(database_id, user_id)`` pair, refreshing it via the engine spec when the
  cached token is expired.  Async (uses :class:`httpx.AsyncClient` for token
  refresh requests).
* :func:`refresh_oauth2_token` — async token refresh helper.
* :func:`check_for_oauth2` — asynccontextmanager with signature
  ``check_for_oauth2(database)``.
* :class:`OAuth2StateSchema` / :class:`OAuth2ClientConfigSchema` — msgspec
  validation structs for OAuth2 state and client config.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, cast, TYPE_CHECKING, TypedDict

import backoff
import jwt
import msgspec
from sqlalchemy import select

from superset.distributed_lock import KeyValueDistributedLock
from superset.exceptions import (
    CreateKeyValueDistributedLockFailedException,
    OAuth2Error,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from superset.config import SupersetSettings
    from superset.db_engine_specs.base import BaseEngineSpec
    from superset.models.core import Database

logger = logging.getLogger(__name__)

# Lifetime of a state token.
JWT_EXPIRATION = timedelta(minutes=5)


class OAuth2ClientConfig(TypedDict):
    """Configuration for an OAuth2 client."""

    id: str
    secret: str
    scope: str
    redirect_uri: str
    authorization_request_uri: str
    token_request_uri: str
    request_content_type: str  # "json" | "data"


class OAuth2TokenResponse(TypedDict, total=False):
    """OAuth2 token endpoint response."""

    access_token: str
    expires_in: int
    scope: str
    token_type: str
    # Only present on the initial code-for-token exchange.
    refresh_token: str


class OAuth2State(TypedDict):
    """State payload round-tripped through the OAuth2 provider."""

    database_id: int
    user_id: int
    default_redirect_uri: str
    tab_id: str


def _get_settings() -> "SupersetSettings":
    """Return a :class:`SupersetSettings` instance.

    Imports lazily to avoid a circular import — :mod:`superset.config`
    imports a number of submodules during construction.
    """
    from superset.config import SupersetSettings

    return SupersetSettings()  # type: ignore[call-arg]


def _get_secret_key() -> str:
    """Return the configured secret key as a plain string."""
    settings = _get_settings()
    raw = settings.secret_key
    if hasattr(raw, "get_secret_value"):
        return raw.get_secret_value()
    return str(raw)


def _get_jwt_algorithm() -> str:
    """Return the JWT algorithm configured for OAuth2 state signing."""
    settings = _get_settings()
    return getattr(settings, "database_oauth2_jwt_algorithm", "HS256") or "HS256"


class OAuth2StateSchema(msgspec.Struct, kw_only=True):
    """Validates the OAuth2 ``state`` payload after JWT decoding.

    Required fields with type validation; extra keys (e.g. ``exp``) are tolerated.
    """

    database_id: int
    user_id: int
    default_redirect_uri: str
    tab_id: str


class OAuth2ClientConfigSchema(msgspec.Struct, kw_only=True):
    """Validates an OAuth2 client config dict."""

    id: str
    secret: str
    scope: str
    authorization_request_uri: str
    token_request_uri: str
    redirect_uri: str | None = None
    request_content_type: str = "json"


_ALLOWED_REQUEST_CONTENT_TYPES = frozenset({"json", "data"})


def _default_oauth2_redirect_uri() -> str:
    """Return the default OAuth2 redirect URI.

    Synthesises the absolute URI from ``WEBDRIVER_BASEURL +
    DATABASE_OAUTH2_REDIRECT_URI`` when no request context is available.
    Falls back to the bare path when no base URL is configured.
    """
    settings = _get_settings()
    override = getattr(settings, "database_oauth2_redirect_uri", "") or ""
    if override.startswith("http://") or override.startswith("https://"):
        return override
    path = override or "/api/v1/database/oauth2/"
    if base := (getattr(settings, "webdriver_baseurl", "") or "").rstrip("/"):
        if not path.startswith("/"):
            path = "/" + path
        return f"{base}{path}"
    return path


def validate_oauth2_client_config(client: dict[str, Any]) -> OAuth2ClientConfig:
    """Validate a raw OAuth2 client config dict and apply defaults.

    Raises :class:`ValueError` when a required field is missing or the
    ``request_content_type`` is not one of ``json``/``data``.
    """
    try:
        validated = msgspec.convert(client, OAuth2ClientConfigSchema)
    except msgspec.ValidationError as ex:
        raise ValueError(f"Invalid OAuth2 client config: {ex}") from ex

    if validated.request_content_type not in _ALLOWED_REQUEST_CONTENT_TYPES:
        raise ValueError(
            "OAuth2 client config 'request_content_type' must be one of "
            f"{sorted(_ALLOWED_REQUEST_CONTENT_TYPES)}, got "
            f"{validated.request_content_type!r}"
        )

    redirect_uri = validated.redirect_uri or _default_oauth2_redirect_uri()
    return OAuth2ClientConfig(
        id=validated.id,
        secret=validated.secret,
        scope=validated.scope,
        redirect_uri=redirect_uri,
        authorization_request_uri=validated.authorization_request_uri,
        token_request_uri=validated.token_request_uri,
        request_content_type=validated.request_content_type,
    )


def encode_oauth2_state(state: dict[str, Any]) -> str:
    """Encode the OAuth2 ``state`` into a signed JWT.

    Signs with ``SECRET_KEY`` + ``DATABASE_OAUTH2_JWT_ALGORITHM`` and stamps an
    ``exp`` claim 5 minutes in the future.  Periods are escaped for
    compatibility with Google OAuth2.
    """
    payload = {
        "exp": datetime.now(tz=timezone.utc) + JWT_EXPIRATION,
        "database_id": state["database_id"],
        "user_id": state["user_id"],
        "default_redirect_uri": state["default_redirect_uri"],
        "tab_id": state["tab_id"],
    }
    encoded_state = jwt.encode(
        payload=payload,
        key=_get_secret_key(),
        algorithm=_get_jwt_algorithm(),
    )

    # Google OAuth2 needs periods to be escaped.
    return encoded_state.replace(".", "%2E")


def decode_oauth2_state(encoded_state: str) -> dict[str, Any]:
    """Decode and validate a JWT produced by :func:`encode_oauth2_state`.

    JWT exceptions (``jwt.ExpiredSignatureError``, ``jwt.InvalidTokenError``)
    propagate to the caller unmodified.
    """
    # Reverse the period-escape applied during encoding.
    encoded_state = encoded_state.replace("%2E", ".")
    payload = jwt.decode(
        jwt=encoded_state,
        key=_get_secret_key(),
        algorithms=[_get_jwt_algorithm()],
    )

    if not isinstance(payload, dict):
        raise OAuth2Error("Invalid OAuth2 state payload")

    # Validate the payload shape via the msgspec schema (drops extra keys
    # like ``exp``).  Mirrors ``oauth2_state_schema.load(payload)`` from
    # the original.
    state_fields = {
        "database_id": payload.get("database_id"),
        "user_id": payload.get("user_id"),
        "default_redirect_uri": payload.get("default_redirect_uri"),
        "tab_id": payload.get("tab_id"),
    }
    try:
        validated = msgspec.convert(state_fields, OAuth2StateSchema)
    except msgspec.ValidationError as ex:
        raise OAuth2Error(f"Invalid OAuth2 state payload: {ex}") from ex

    return {
        "database_id": validated.database_id,
        "user_id": validated.user_id,
        "default_redirect_uri": validated.default_redirect_uri,
        "tab_id": validated.tab_id,
    }


@backoff.on_exception(
    backoff.expo,
    CreateKeyValueDistributedLockFailedException,
    factor=10,
    base=2,
    max_tries=5,
)
async def get_oauth2_access_token(
    config: OAuth2ClientConfig,
    database_id: int,
    user_id: int,
    db_engine_spec: type["BaseEngineSpec"],
    session: "AsyncSession",
) -> str | None:
    """Return a valid OAuth2 access token, refreshing it on demand.

    * Looks up the row in ``database_user_oauth2_tokens``.
    * Returns the cached ``access_token`` when still valid.
    * If expired but a ``refresh_token`` is available, refreshes the token
      via the engine spec inside a KV-backed distributed lock so concurrent
      callers serialise on the IDP exchange (avoids losing a rotated refresh
      token).  The ``@backoff.on_exception`` decorator retries when another
      worker holds the lock — same retry policy as the sync original
      (``factor=10, base=2, max_tries=5``).
    * Otherwise deletes the stale row and returns ``None`` so the caller
      can trigger the OAuth2 dance.
    """
    # pylint: disable=import-outside-toplevel
    from superset.models.core import DatabaseUserOAuth2Tokens

    stmt = select(DatabaseUserOAuth2Tokens).where(
        DatabaseUserOAuth2Tokens.user_id == user_id,
        DatabaseUserOAuth2Tokens.database_id == database_id,
    )
    result = await session.execute(stmt)
    token = result.scalars().one_or_none()
    if token is None:
        return None

    if (
        token.access_token
        and token.access_token_expiration is not None
        and datetime.now() < token.access_token_expiration
    ):
        return str(token.access_token) if token.access_token is not None else None

    if token.refresh_token:
        return await refresh_oauth2_token(
            config,
            database_id,
            user_id,
            db_engine_spec,
            token,
            session,
        )

    # Expired access token and no refresh token — drop the row so the
    # caller starts a fresh dance.
    await session.delete(token)
    return None


async def refresh_oauth2_token(
    config: OAuth2ClientConfig,
    database_id: int,
    user_id: int,
    db_engine_spec: type["BaseEngineSpec"],
    token: Any,  # DatabaseUserOAuth2Tokens — typed via Any to avoid cyclical import
    session: "AsyncSession",
) -> str | None:
    """Use the refresh token to get a new access token and persist it.

    The IDP exchange + DB write are performed under a KV-backed distributed
    lock so concurrent dashboards refreshing the same ``(user_id, database_id)``
    pair don't all hit the IDP and risk losing a rotated refresh token.
    """
    async with KeyValueDistributedLock(
        namespace="refresh_oauth2_token",
        session=session,
        user_id=user_id,
        database_id=database_id,
    ):
        token_response = await db_engine_spec.get_oauth2_fresh_token(
            dict(config),
            token.refresh_token,
        )
        # Refresh token may have been revoked — let the caller restart
        # the dance.
        if "access_token" not in token_response:
            logger.warning(
                "OAuth2 refresh failed for database_id=%s user_id=%s",
                token.database_id,
                token.user_id,
            )
            return None

        token.access_token = token_response["access_token"]
        token.access_token_expiration = datetime.now() + timedelta(
            seconds=token_response["expires_in"]
        )
        session.add(token)
        await session.flush()
        return token.access_token


def sync_refresh_oauth2_token(
    config: "OAuth2ClientConfig",
    database_id: int,
    user_id: int,
    db_engine_spec: type["BaseEngineSpec"],
    token: Any,  # DatabaseUserOAuth2Tokens — Any to avoid a cyclical import
    session: Any,  # sqlalchemy.orm.Session (the same session that loaded *token*)
) -> str | None:
    """Use the refresh token to get a new access token and persist it (sync).

    The IDP exchange + DB write run under a KV-backed distributed lock so
    concurrent sync refreshes for the same ``(user_id, database_id)`` pair
    don't all hit the IDP and risk losing a rotated refresh token.

    ``token`` must be attached to ``session`` — the caller
    (:func:`sync_get_oauth2_access_token`) loads it in the same session it
    passes here, so the ``add`` + ``commit`` below persist without a
    detached-instance error. Uses the sync IDP fetch
    (:meth:`BaseEngineSpec.get_oauth2_fresh_token_sync`) and the sync lock
    (:func:`superset.distributed_lock.sync_key_value_distributed_lock`).
    """
    # pylint: disable=import-outside-toplevel
    from superset.distributed_lock import sync_key_value_distributed_lock

    with sync_key_value_distributed_lock(
        namespace="refresh_oauth2_token",
        user_id=user_id,
        database_id=database_id,
    ):
        token_response = db_engine_spec.get_oauth2_fresh_token_sync(
            dict(config),
            token.refresh_token,
        )

        # The refresh token may have been revoked — let the caller restart
        # the dance.
        if "access_token" not in token_response:
            logger.warning(
                "OAuth2 refresh failed for database_id=%s user_id=%s",
                database_id,
                user_id,
            )
            return None

        token.access_token = token_response["access_token"]
        token.access_token_expiration = datetime.now() + timedelta(
            seconds=token_response["expires_in"]
        )
        session.add(token)
        session.commit()
        return token.access_token


def sync_get_oauth2_access_token(
    config: "OAuth2ClientConfig",
    database_id: int,
    user_id: int,
    db_engine_spec: type["BaseEngineSpec"],
) -> str | None:
    """Synchronous OAuth2 access-token lookup for the connection path.

    Reads ``database_user_oauth2_tokens`` via the sync metadata session
    (``get_sync_session`` / psycopg2).  Used by :func:`get_sync_engine`
    to thread a per-user OAuth2 Bearer into impersonation — the OAuth2
    engines (Trino / BigQuery / Snowflake / Databricks / GSheets) are
    sync-only, so this is the connection path that matters.

    * Returns the cached ``access_token`` when still valid.
    * If expired but a ``refresh_token`` is present, refreshes it inline via
      :func:`sync_refresh_oauth2_token` (under the sync distributed lock) and
      returns the new token — within the *same* session that loaded the row,
      so the persist in ``sync_refresh_oauth2_token`` is not detached.
    * Otherwise deletes the stale (non-refreshable) row and returns ``None``
      so the OAuth2 dance re-triggers.
    """
    # pylint: disable=import-outside-toplevel
    from superset.db.session import get_sync_session
    from superset.models.core import DatabaseUserOAuth2Tokens

    try:
        with get_sync_session() as session:
            token = (
                session.query(DatabaseUserOAuth2Tokens)
                .filter_by(user_id=user_id, database_id=database_id)
                .one_or_none()
            )
            if token is None:
                return None
            if (
                token.access_token
                and token.access_token_expiration
                and datetime.now() < token.access_token_expiration
            ):
                return cast("str | None", token.access_token)
            # Expired but refreshable: refresh inline.
            if token.refresh_token:
                return sync_refresh_oauth2_token(
                    config,
                    database_id,
                    user_id,
                    db_engine_spec,
                    token,
                    session,
                )
            # Expired and no refresh token: drop the row so the OAuth2
            # dance re-triggers.
            session.delete(token)
            session.commit()
            return None
    except Exception:  # noqa: BLE001
        logger.debug("sync OAuth2 access-token lookup failed", exc_info=True)
        return None


@asynccontextmanager
async def check_for_oauth2(database: "Database") -> AsyncIterator[None]:
    """Run async code and trigger the OAuth2 dance on driver-level failure.

    Async contextmanager; the body may emit awaits and ``start_oauth2_dance``
    is async (it raises ``OAuth2RedirectError`` after building the
    authorization URL).

    Usage::

        async with check_for_oauth2(database):
            await conn.execute(text("SELECT 1"))
    """
    try:
        yield
    except Exception as ex:
        if database.is_oauth2_enabled() and database.db_engine_spec.needs_oauth2(ex):
            await database.db_engine_spec.start_oauth2_dance(database)
        raise


def get_oauth2_timeout() -> timedelta:
    """Return the configured OAuth2 HTTP-request timeout."""
    settings = _get_settings()
    seconds = int(getattr(settings, "database_oauth2_timeout", 30))
    return timedelta(seconds=seconds)


def get_oauth2_clients() -> dict[str, dict[str, Any]]:
    """Return the configured ``DATABASE_OAUTH2_CLIENTS`` mapping."""
    return getattr(_get_settings(), "database_oauth2_clients", {}) or {}


def get_default_oauth2_redirect_uri() -> str:
    """Return the configured default OAuth2 redirect URI.

    Falls back to ``/api/v1/database/oauth2/`` (relative) when no explicit
    override is configured — engine specs may pass an absolute callback
    URI when registering with the IDP.
    """
    settings = _get_settings()
    override = getattr(settings, "database_oauth2_redirect_uri", "")
    return override or "/api/v1/database/oauth2/"


# Issued so that ``utc`` import does not get optimised away by linters when
# this module is imported in environments where the timezone is unused.
_ = timezone
