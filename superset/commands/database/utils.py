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
"""Async port of ``superset_old/commands/database/utils.py``.

Local helper utilities shared between database commands.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from typing import Any

from superset.exceptions import CommandInvalidError

logger = logging.getLogger(__name__)

EXPORT_VERSION = "1.0.0"

# Regex to sanitize file names (remove unsafe characters)
_SAFE_FILENAME_RE = re.compile(r"[^\w\s\-.]")

PASSWORD_MASK = "XXXXXXXXXX"  # noqa: S105

# Field-length bounds — 1:1 with the original Marshmallow ``Length`` validators
# (superset_old/databases/schemas.py).
DATABASE_NAME_MAX_LEN = 250
FORCE_CTAS_SCHEMA_MAX_LEN = 250
SQLALCHEMY_URI_MAX_LEN = 1024


def _validate_sqlalchemy_uri_safety(uri: str) -> None:
    """Validate a SQLAlchemy URI string for parse + connection safety.

    Ported 1:1 from ``superset_old/databases/schemas.py::sqlalchemy_uri_validator``:

    * Parse the URI through ``make_url_safe`` — an unparseable URI raises a
      400-equivalent error.
    * When ``PREVENT_UNSAFE_DB_CONNECTIONS`` is enabled (default), run the
      configurable ``check_sqlalchemy_uri`` allowlist (rejects sqlite /
      shillelagh / meta-DB dialects) instead of a hardcoded scheme set.
    """
    if not uri:
        return

    from superset.config import SupersetSettings
    from superset.databases.utils import DatabaseInvalidError, make_url_safe
    from superset.exceptions import SupersetSecurityException
    from superset.security.analytics_db_safety import check_sqlalchemy_uri

    try:
        url = make_url_safe(uri.strip())
    except DatabaseInvalidError as ex:
        raise CommandInvalidError(
            "Invalid connection string, a valid string usually follows: "
            "backend+driver://user:password@database-host/database-name"
        ) from ex

    settings = SupersetSettings()  # type: ignore[call-arg]
    if getattr(settings, "prevent_unsafe_db_connections", True):
        try:
            check_sqlalchemy_uri(url)
        except SupersetSecurityException as ex:
            raise CommandInvalidError(str(ex)) from ex


def _validate_extra(value: str | None) -> None:
    """Validate the ``extra`` JSON field.

    Ported 1:1 from ``superset_old/databases/schemas.py::extra_validator``:
    ``extra`` must be valid JSON and every ``metadata_params`` key must be a
    valid keyword argument of SQLAlchemy's ``MetaData`` constructor.
    """
    if not value:
        return
    from sqlalchemy import MetaData

    try:
        extra_ = json.loads(value)
    except json.JSONDecodeError as ex:
        raise CommandInvalidError(f"Field cannot be decoded by JSON. {ex}") from ex

    # ``extra`` must be a JSON object. Upstream's ``extra_validator`` calls
    # ``extra_.get("metadata_params", …)`` unguarded, so a valid-but-non-object
    # value (``[1,2]`` / ``5`` / ``"s"``) raises ``AttributeError`` → HTTP 500.
    # Reject it as a clean 4xx instead (consistent with the metadata_params
    # validation below, which already returns 422 for malformed input).
    if extra_ is not None and not isinstance(extra_, dict):
        raise CommandInvalidError("The Extra field must be a JSON object.")

    metadata_signature = inspect.signature(MetaData)
    for key in (extra_ or {}).get("metadata_params", {}):
        if key not in metadata_signature.parameters:
            raise CommandInvalidError(
                "The metadata_params in Extra field is not configured "
                f"correctly. The key {key} is invalid."
            )


def _validate_server_cert(value: str | None) -> None:
    """Validate the ``server_cert`` PEM certificate.

    Ported 1:1 from ``superset_old/databases/schemas.py::server_cert_validator``.
    """
    if not value:
        return
    from superset.exceptions import CertificateException
    from superset.utils.core import parse_ssl_cert

    try:
        parse_ssl_cert(value)
    except CertificateException as ex:
        raise CommandInvalidError("Invalid certificate") from ex


def _validate_field_lengths(
    *,
    database_name: str | None = None,
    sqlalchemy_uri: str | None = None,
    force_ctas_schema: str | None = None,
    sqlalchemy_uri_min: int = 1,
) -> None:
    """Validate field-length bounds.

    Ported 1:1 from the original ``Length`` Marshmallow validators
    (superset_old/databases/schemas.py): ``database_name`` 1-250,
    ``force_ctas_schema`` 0-250, ``sqlalchemy_uri`` min..1024 (min is 1 on
    POST, 0 on PUT — passed via ``sqlalchemy_uri_min``).
    """
    if database_name is not None and not 1 <= len(database_name) <= (
        DATABASE_NAME_MAX_LEN
    ):
        raise CommandInvalidError(
            f"Length must be between 1 and {DATABASE_NAME_MAX_LEN}."
        )
    if force_ctas_schema is not None and len(force_ctas_schema) > (
        FORCE_CTAS_SCHEMA_MAX_LEN
    ):
        raise CommandInvalidError(
            f"Longer than maximum length {FORCE_CTAS_SCHEMA_MAX_LEN}."
        )
    if sqlalchemy_uri is not None and not (
        sqlalchemy_uri_min <= len(sqlalchemy_uri) <= SQLALCHEMY_URI_MAX_LEN
    ):
        raise CommandInvalidError(
            f"Length must be between {sqlalchemy_uri_min} and {SQLALCHEMY_URI_MAX_LEN}."
        )


def _safe_filename(name: str) -> str:
    """Create a safe filename from a model name (like a secure-filename helper)."""
    name = _SAFE_FILENAME_RE.sub("", name).strip()
    return name or "unnamed"


def _parse_extra(extra_payload: str) -> dict[str, Any]:
    """Parse the extra JSON field from a Database, with legacy fixups."""
    try:
        extra = json.loads(extra_payload)
    except (json.JSONDecodeError, TypeError):
        return {}
    # Fix for DBs saved with an invalid ``schemas_allowed_for_csv_upload``
    schemas_allowed = extra.get("schemas_allowed_for_csv_upload")
    if isinstance(schemas_allowed, str):
        try:
            extra["schemas_allowed_for_csv_upload"] = json.loads(schemas_allowed)
        except (json.JSONDecodeError, TypeError):
            pass
    return extra


def _mask_ssh_tunnel_passwords(payload: dict[str, Any]) -> dict[str, Any]:
    """Mask password fields in an SSH tunnel export payload."""
    masked = dict(payload)
    for key in ("password", "private_key", "private_key_password"):
        if masked.get(key):
            masked[key] = PASSWORD_MASK
    return masked


async def add_permissions(
    database: Any,
    security_manager: Any,
    ssh_tunnel: Any | None = None,
) -> None:
    """Add DAR (data-access-role) for the database's catalogs and schemas.

    Async port of ``superset_old/commands/database/utils.py::add_permissions``
    (called from ``CreateDatabaseCommand.run`` after the DB is created). For a
    newly-created database this enumerates the connection's catalogs/schemas and
    creates the ``catalog_access`` / ``schema_access`` permission-view-menus so
    that per-catalog/per-schema RBAC grants can be made immediately.

    Faithful to the original:

    * For catalog-aware engines, permissions are added for every catalog when
      cross-catalog queries are supported (or ``allow_multi_catalog`` is set),
      otherwise only for the default catalog.
    * Schema enumeration is best-effort and per-catalog: a failure to introspect
      one catalog logs a warning and continues (the original swallows
      ``GenericDBException``; here any introspection error is swallowed because
      the new ``Database`` model surfaces driver errors through the sync
      inspector run in a thread).

    Unlike the original (which used the blocking ``Database.get_all_*_names``)
    the port runs the blocking sync inspector via ``asyncio.to_thread`` and uses
    the async ``security_manager.add_permission_view_menu`` to create the PVM
    rows on the active ``AsyncSession``.

    The ``ssh_tunnel`` (the tunnel just created in the same transaction) is
    threaded through ``Database.get_inspector(ssh_tunnel=...)`` so catalog/schema
    enumeration works for tunnel-only databases. The port's ``get_sync_engine``
    only honours an *explicit* ``override_ssh_tunnel`` and does not auto-resolve
    the stored tunnel (which, for a just-created DB inside the same transaction,
    is not yet committed/visible to a fresh lookup anyway) — matching the
    original, which passed ``ssh_tunnel`` to ``get_all_*_names`` →
    ``get_inspector`` → ``get_sqla_engine(override_ssh_tunnel=ssh_tunnel)``.
    """
    import asyncio

    db_engine_spec = database.db_engine_spec

    if getattr(db_engine_spec, "supports_catalog", False):
        # Adding permissions to all catalogs (and all their schemas) can take a
        # long time. If the database does not support cross-catalog queries
        # (like Postgres), and the multi-catalog feature is not enabled, then we
        # only need to add permissions to the default catalog.
        if getattr(db_engine_spec, "supports_cross_catalog_queries", False) or getattr(
            database, "allow_multi_catalog", False
        ):

            def _fetch_catalogs() -> set[str | None]:
                with database.get_inspector(ssh_tunnel=ssh_tunnel) as inspector:
                    return db_engine_spec.get_catalog_names(database, inspector)

            try:
                catalogs: set[str | None] = await asyncio.to_thread(_fetch_catalogs)
            except Exception:  # noqa: BLE001
                logger.warning("Failed to get catalog names", exc_info=True)
                catalogs = {database.get_default_catalog()}
        else:
            catalogs = {database.get_default_catalog()}

        for catalog in catalogs:
            await security_manager.add_permission_view_menu(
                "catalog_access",
                security_manager.get_catalog_perm(
                    database.database_name,
                    catalog,
                ),
            )
    else:
        catalogs = {None}

    for catalog in catalogs:
        try:

            def _fetch_schemas(catalog: str | None = catalog) -> set[str]:
                with database.get_inspector(
                    catalog=catalog, ssh_tunnel=ssh_tunnel
                ) as inspector:
                    return db_engine_spec.get_schema_names(inspector)

            schemas = await asyncio.to_thread(_fetch_schemas)
            for schema in schemas:
                await security_manager.add_permission_view_menu(
                    "schema_access",
                    security_manager.get_schema_perm(
                        database.database_name,
                        schema,
                        catalog=catalog,
                    ),
                )
        except Exception:  # noqa: BLE001
            logger.warning("Error processing catalog '%s'", catalog, exc_info=True)
            continue
