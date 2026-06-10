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
"""Async port of ``superset_old.commands.importers.v1.utils``.

Provides the bundle-level helpers used by every per-resource import command:

* :func:`load_yaml` — wrap ``yaml.safe_load`` with file-name attribution.
* :func:`load_metadata` — read & validate ``metadata.yaml``.
* :func:`validate_metadata_type` — assert the manifest's ``type`` field
  matches the importer's expected type.
* :func:`load_configs` — schema-validate every bundle entry, splice in
  passwords/SSH-tunnel secrets keyed by UUID, normalise example URLs.
* :func:`is_valid_config` — filter out hidden files & non-YAML entries.
* :func:`get_contents_from_bundle` — read a ZIP into ``{filename: text}``.
* :func:`get_resource_mappings_batched` — async batch fetch of UUID -> id
  (or arbitrary value) mappings for sparse imports.
* :func:`import_tag` — UUID-keyed tag upsert mirroring the original.
* ``Importv1*Schema`` callables — full-field validators ported from the
  Marshmallow schemas in ``superset_old.<resource>.schemas``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any
from zipfile import ZipFile

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from superset.commands.importers.exceptions import (
    IncorrectFormatError,
    IncorrectVersionError,
)
from superset.exceptions import CommandInvalidError, SupersetException
from superset.i18n import gettext as _

logger = logging.getLogger(__name__)

METADATA_FILE_NAME = "metadata.yaml"
IMPORT_VERSION = "1.0.0"

# Maximum number of entries we will inspect in a bundle.  Mirrors
# ``superset_old.utils.core.check_is_safe_zip`` which uses 1000.
_MAX_ZIP_ENTRIES = 1000


# --------------------------------------------------------------------------- #
# YAML / metadata helpers
# --------------------------------------------------------------------------- #


def remove_root(file_path: str) -> str:
    """Strip the first directory of a ZIP path.

    Verbatim port of ``superset_old.commands.importers.v1.utils.remove_root``.
    """
    full_path = PurePosixPath(file_path)
    relative_path = PurePosixPath(*full_path.parts[1:])
    return str(relative_path)


def load_yaml(file_name: str, content: str) -> Any:
    """Try to load a YAML file, raising :class:`CommandInvalidError` on parse error.

    Mirrors the original — accepts dicts, lists, ``None`` (returned as-is) and
    propagates parse errors as a key-attributed :class:`CommandInvalidError`.
    """
    try:
        return yaml.safe_load(content)
    except yaml.YAMLError as ex:
        logger.exception("Invalid YAML in %s", file_name)
        raise CommandInvalidError(
            _("%(file)s: Not a valid YAML file", file=file_name)
        ) from ex


def load_metadata(contents: dict[str, str]) -> dict[str, str]:
    """Apply validation and load a metadata file.

    Raises :class:`IncorrectVersionError` *only* when the bundle is
    missing the ``metadata.yaml`` file or when its ``version`` field
    doesn't match :data:`IMPORT_VERSION` — that signals the dispatcher to
    try a different command version. Other validation problems are
    surfaced as :class:`CommandInvalidError` whose message is the
    Marshmallow-style ``{METADATA_FILE_NAME: {...}}`` shape, so frontends
    that parse the nested dict keep working.
    """
    if METADATA_FILE_NAME not in contents:
        # if the contents have no METADATA_FILE_NAME this is probably
        # a original export without versioning that should not be
        # handled by this command
        raise IncorrectVersionError(f"Missing {METADATA_FILE_NAME}")

    metadata = load_yaml(METADATA_FILE_NAME, contents[METADATA_FILE_NAME])
    if not isinstance(metadata, dict):
        raise CommandInvalidError(
            {METADATA_FILE_NAME: {"_schema": ["Not a valid mapping"]}}
        )

    errors: dict[str, list[str]] = {}

    # ``version`` field — Equal validator: raise IncorrectVersionError for
    # ANY version-field problem (missing OR wrong value) so the dispatcher
    # can try a different command version.  This mirrors the original
    # Marshmallow path: required=True puts "version" in ex.messages for a
    # missing field too, and the guard ``if "version" in ex.messages`` then
    # raises IncorrectVersionError in both cases.
    version = metadata.get("version")
    if version is None:
        # Missing field — mirrors Marshmallow required=True message.
        raise IncorrectVersionError("Missing data for required field.")
    if version != IMPORT_VERSION:
        # Wrong value — match the original message verbatim ("Must be equal
        # to <expected>.") — frontends pattern-match on this string.
        raise IncorrectVersionError(f"Must be equal to {IMPORT_VERSION}.")

    # ``type`` is not required at this layer, but if present must be a string.
    if "type" in metadata and not isinstance(metadata["type"], str):
        errors.setdefault("type", []).append("Not a valid string.")

    # ``timestamp`` is optional — only validate format if present.
    # NOTE: a non-string (incl. the ``datetime`` objects PyYAML produces for
    # unquoted timestamp literals) is REJECTED — Marshmallow's
    # ``DateTime._deserialize`` calls ``from_iso_datetime`` whose regex
    # ``.match`` raises TypeError on non-strings → "Not a valid datetime.".
    timestamp = metadata.get("timestamp")
    if timestamp is not None and not isinstance(timestamp, str):
        errors.setdefault("timestamp", []).append("Not a valid datetime.")

    if errors:
        raise CommandInvalidError({METADATA_FILE_NAME: errors})

    return metadata


def validate_metadata_type(
    metadata: dict[str, str] | None,
    type_: str,
    exceptions: list[Exception],
) -> None:
    """Validate that the type declared in ``metadata.yaml`` matches the importer.

    On mismatch, append a :class:`CommandInvalidError` whose message has
    the original Marshmallow ``{file_name: {field: [errors]}}`` shape.
    """
    if metadata and "type" in metadata and metadata["type"] != type_:
        exceptions.append(
            CommandInvalidError(
                {METADATA_FILE_NAME: {"type": [f"Must be equal to {type_}."]}}
            )
        )


# --------------------------------------------------------------------------- #
# Bundle / ZIP helpers
# --------------------------------------------------------------------------- #


def is_valid_config(file_name: str) -> bool:
    """Return ``True`` if the bundle entry is a YAML config we should import.

    Verbatim port of ``superset_old.commands.importers.v1.utils.is_valid_config``.
    """
    path = Path(file_name)

    # ignore system files that might've been added to the bundle
    if path.name.startswith(".") or path.name.startswith("_"):
        return False

    # ensure extension is YAML
    if path.suffix.lower() not in {".yaml", ".yml"}:
        return False

    return True


def _check_is_safe_zip(zip_file: ZipFile) -> None:
    """Verify a ZIP archive's entries satisfy our safety constraints.

    1:1 port of :func:`superset_old.utils.core.check_is_safe_zip`:
    - Rejects any individual entry whose uncompressed size exceeds
      ``ZIPPED_FILE_MAX_SIZE`` (default 100 MB).
    - Rejects the archive when the overall compression ratio exceeds
      ``ZIP_FILE_MAX_COMPRESS_RATIO`` (default 200×) — zip-bomb guard.
    - Additionally enforces the entry-count cap (1000) and path-traversal
      rejection added in the liteset port.
    """
    # pylint: disable=import-outside-toplevel
    from superset.config import SupersetSettings

    try:
        settings = SupersetSettings()  # type: ignore[call-arg]
        zipped_file_max_size: int = getattr(
            settings, "zipped_file_max_size", 100 * 1024 * 1024
        )
        zip_file_max_compress_ratio: float = getattr(
            settings, "zip_file_max_compress_ratio", 200.0
        )
    except Exception:  # noqa: BLE001
        zipped_file_max_size = 100 * 1024 * 1024
        zip_file_max_compress_ratio = 200.0

    entries = zip_file.namelist()
    if len(entries) > _MAX_ZIP_ENTRIES:
        raise IncorrectFormatError(
            _(
                "ZIP contains too many entries: %(count)d > %(max)d",
                count=len(entries),
                max=_MAX_ZIP_ENTRIES,
            )
        )
    for name in entries:
        parts = PurePosixPath(name).parts
        if ".." in parts:
            raise IncorrectFormatError(
                _("ZIP entry contains path traversal: %(name)s", name=name)
            )

    uncompress_size = 0
    compress_size = 0
    for zip_info in zip_file.infolist():
        if zip_info.file_size > zipped_file_max_size:
            raise SupersetException("Found file with size above allowed threshold")
        uncompress_size += zip_info.file_size
        compress_size += zip_info.compress_size
    if compress_size and uncompress_size / compress_size > zip_file_max_compress_ratio:
        raise SupersetException("Zip compress ratio above allowed threshold")


def get_contents_from_bundle(bundle: ZipFile) -> dict[str, str]:
    """Return ``{filename: text-content}`` for every YAML entry in ``bundle``.

    Strips the leading directory (matching the original ``remove_root``
    behaviour — exports always nest everything inside a top-level folder
    named after the bundle) and skips non-YAML / hidden files.
    """
    _check_is_safe_zip(bundle)
    contents: dict[str, str] = {}
    for file_name in bundle.namelist():
        if not is_valid_config(file_name):
            continue
        contents[remove_root(file_name)] = bundle.read(file_name).decode("utf-8")
    return contents


# --------------------------------------------------------------------------- #
# Schema validation + secret splicing
# --------------------------------------------------------------------------- #


# Schema callable type: takes a config dict, returns ``None`` on success,
# or raises :class:`CommandInvalidError` on validation failure.
SchemaCallable = Callable[[dict[str, Any]], None]


def _normalize_example_data_url(url: str) -> str:
    """Rewrite legacy ``raw.githubusercontent.com`` example dataset URLs."""
    try:
        from superset.examples.helpers import normalize_example_data_url
    except ImportError:
        return url
    return normalize_example_data_url(url)


_PASSWORD_MASK = "XXXXXXXXXX"  # noqa: S105


async def _existing_database_secrets(
    session: AsyncSession,
) -> tuple[
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
]:
    """Load existing database & SSH tunnel secrets keyed by UUID."""
    from superset.models.core import Database

    try:
        from superset.models.ssh_tunnel import SSHTunnel
    except ImportError:
        SSHTunnel = None  # type: ignore[assignment,misc]  # noqa: N806  # class alias

    db_passwords: dict[str, str] = {}
    db_rows = (await session.execute(select(Database.uuid, Database.password))).all()
    for uuid_val, password in db_rows:
        if uuid_val is not None:
            db_passwords[str(uuid_val)] = password or ""

    ssh_passwords: dict[str, str] = {}
    ssh_pkeys: dict[str, str] = {}
    ssh_pk_passwords: dict[str, str] = {}

    if SSHTunnel is not None:
        ssh_rows = (
            await session.execute(
                select(
                    SSHTunnel.uuid,
                    SSHTunnel.password,
                    SSHTunnel.private_key,
                    SSHTunnel.private_key_password,
                )
            )
        ).all()
        for uuid_val, password, pkey, pkey_pw in ssh_rows:
            if uuid_val is None:
                continue
            ssh_passwords[str(uuid_val)] = password or ""
            ssh_pkeys[str(uuid_val)] = pkey or ""
            ssh_pk_passwords[str(uuid_val)] = pkey_pw or ""

    return db_passwords, ssh_passwords, ssh_pkeys, ssh_pk_passwords


def _validate_database_masked_credentials(  # noqa: C901
    config: dict[str, Any],
    file_name: str,
    existing_uuids: dict[str, str],
) -> None:
    """Raise :class:`CommandInvalidError` when a NEW database bundle entry
    contains masked passwords / SSH-tunnel credentials without real values.

    1:1 port of the Marshmallow ``@validates_schema`` methods
    ``validate_password`` and ``validate_ssh_tunnel_credentials`` from
    ``superset_old/databases/schemas.py:873-946``.

    Only applies to databases that do NOT already exist (existing entries
    keep their stored secrets — the caller splices those in before calling
    this function).  ``existing_uuids`` is the UUID→password dict built
    from the current DB rows in :func:`_existing_database_secrets`.
    """
    uuid = config.get("uuid")
    if uuid and str(uuid) in existing_uuids:
        # Database already exists — keep stored secrets, no validation needed.
        return

    # validate_password: if the URI's embedded password is the mask and no
    # explicit ``password`` override was provided in the request, reject.
    from superset.databases.utils import make_url_safe

    try:
        uri_password = make_url_safe(config.get("sqlalchemy_uri", "")).password
    except Exception:  # noqa: BLE001
        uri_password = None
    if uri_password == _PASSWORD_MASK and config.get("password") is None:
        raise CommandInvalidError(
            {file_name: {"password": ["Must provide a password for the database"]}}
        )

    # validate_ssh_tunnel_credentials: check SSH tunnel credential masking.
    ssh_tunnel = config.get("ssh_tunnel")
    if not ssh_tunnel:
        return

    try:
        from superset.utils.feature_flags import feature_flag_manager

        if not feature_flag_manager.is_feature_enabled("SSH_TUNNELING"):
            from superset.commands.database.ssh_tunnel.exceptions import (
                SSHTunnelingNotEnabledError,
            )

            raise SSHTunnelingNotEnabledError()
    except ImportError:
        pass

    password = ssh_tunnel.get("password")
    private_key = ssh_tunnel.get("private_key")
    private_key_password = ssh_tunnel.get("private_key_password")

    if password is not None:
        # Login method #1 (password) — must not mix with key-based method.
        if private_key is not None or private_key_password is not None:
            from superset.commands.database.ssh_tunnel.exceptions import (
                SSHTunnelInvalidCredentials,
            )

            raise SSHTunnelInvalidCredentials()
        if password == _PASSWORD_MASK:
            raise CommandInvalidError(
                {
                    file_name: {
                        "ssh_tunnel": {
                            "password": ["Must provide a password for the ssh tunnel"]
                        }
                    }
                }
            )
    else:
        # Login method #2 (private key + key password).
        if private_key is None and private_key_password is None:
            from superset.commands.database.ssh_tunnel.exceptions import (
                SSHTunnelMissingCredentials,
            )

            raise SSHTunnelMissingCredentials()

        exception_messages: list[str] = []
        if private_key is None or private_key == _PASSWORD_MASK:
            exception_messages.append("Must provide a private key for the ssh tunnel")
        if private_key_password is None or private_key_password == _PASSWORD_MASK:
            exception_messages.append(
                "Must provide a private key password for the ssh tunnel"
            )
        if exception_messages:
            raise CommandInvalidError({file_name: {"ssh_tunnel": exception_messages}})


async def load_configs(  # noqa: C901
    contents: dict[str, str],
    schemas: dict[str, SchemaCallable],
    passwords: dict[str, str],
    exceptions: list[Exception],
    ssh_tunnel_passwords: dict[str, str],
    ssh_tunnel_private_keys: dict[str, str],
    ssh_tunnel_priv_key_passwords: dict[str, str],
    session: AsyncSession,
) -> dict[str, Any]:
    """Validate every YAML in the bundle and splice in masked secrets.

    Async port of
    ``superset_old.commands.importers.v1.utils.load_configs``.
    Schema validation failures attach the file name as the key (mirrors the
    original ``ex.messages = {file_name: ex.messages}``) so the controller
    layer surfaces ``{<file>: {<field>: [<msg>]}}``.
    """
    configs: dict[str, Any] = {}

    (
        db_passwords,
        db_ssh_tunnel_passwords,
        db_ssh_tunnel_private_keys,
        db_ssh_tunnel_priv_key_passws,
    ) = await _existing_database_secrets(session)

    for file_name, content in contents.items():
        # skip directories
        if not content:
            continue

        prefix = file_name.split("/")[0]
        schema = schemas.get(f"{prefix}/")
        if schema is None:
            continue
        try:
            config = load_yaml(file_name, content)
            if not isinstance(config, dict):
                raise CommandInvalidError(
                    {file_name: {"_schema": ["Not a valid mapping"]}}
                )

            # populate passwords from the request or from existing DBs
            if file_name in passwords:
                config["password"] = passwords[file_name]
            elif prefix == "databases" and config.get("uuid") in db_passwords:
                config["password"] = db_passwords[config["uuid"]]

            # populate ssh_tunnel_passwords from the request or from existing DBs
            if file_name in ssh_tunnel_passwords:
                config.setdefault("ssh_tunnel", {})
                config["ssh_tunnel"]["password"] = ssh_tunnel_passwords[file_name]
            elif (
                prefix == "databases"
                and config.get("uuid") in db_ssh_tunnel_passwords
                and config.get("ssh_tunnel") is not None
            ):
                config["ssh_tunnel"]["password"] = db_ssh_tunnel_passwords[
                    config["uuid"]
                ]

            # populate ssh_tunnel_private_keys from the request or from existing DBs
            if file_name in ssh_tunnel_private_keys:
                config.setdefault("ssh_tunnel", {})
                config["ssh_tunnel"]["private_key"] = ssh_tunnel_private_keys[file_name]
            elif (
                prefix == "databases"
                and config.get("uuid") in db_ssh_tunnel_private_keys
                and config.get("ssh_tunnel") is not None
            ):
                config["ssh_tunnel"]["private_key"] = db_ssh_tunnel_private_keys[
                    config["uuid"]
                ]

            # populate ssh_tunnel_priv_key_passwords from request or existing DBs
            if file_name in ssh_tunnel_priv_key_passwords:
                config.setdefault("ssh_tunnel", {})
                config["ssh_tunnel"]["private_key_password"] = (
                    ssh_tunnel_priv_key_passwords[file_name]
                )
            elif (
                prefix == "databases"
                and config.get("uuid") in db_ssh_tunnel_priv_key_passws
                and config.get("ssh_tunnel") is not None
            ):
                config["ssh_tunnel"]["private_key_password"] = (
                    db_ssh_tunnel_priv_key_passws[config["uuid"]]
                )

            # Normalize example data URLs before schema validation
            if prefix == "datasets" and "data" in config:
                config["data"] = _normalize_example_data_url(config["data"])

            schema(config)

            # Masked-credential validation for database bundles.
            # 1:1 with ``ImportV1DatabaseSchema.validate_password`` and
            # ``validate_ssh_tunnel_credentials`` from
            # ``superset_old/databases/schemas.py``.
            # Only applies to NEW databases (existing ones keep stored secrets).
            if prefix == "databases":
                _validate_database_masked_credentials(config, file_name, db_passwords)

            configs[file_name] = config
        except CommandInvalidError as exc:
            logger.error(
                "Schema validation failed for %s (prefix: %s): %s",
                file_name,
                prefix,
                exc,
            )
            # Wrap message under the file name to mirror Marshmallow shape.
            msg = exc.message if hasattr(exc, "message") else str(exc)
            if isinstance(msg, dict) and file_name in msg:
                exceptions.append(exc)
            else:
                exceptions.append(CommandInvalidError({file_name: msg}))

    return configs


# --------------------------------------------------------------------------- #
# Sparse-import helpers
# --------------------------------------------------------------------------- #


async def get_resource_mappings_batched(
    session: AsyncSession,
    model_class: type[Any],
    batch_size: int = 1000,
    value_func: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    """Async port of ``get_resource_mappings_batched``."""
    if value_func is None:

        def value_func(row: Any) -> int:
            return row.id

    offset = 0
    mapping: dict[str, Any] = {}
    while True:
        stmt = select(model_class).limit(batch_size).offset(offset)
        result = (await session.execute(stmt)).scalars().all()
        if not result:
            break
        mapping.update({str(row.uuid): value_func(row) for row in result})
        offset += batch_size
    return mapping


# --------------------------------------------------------------------------- #
# Schema validators (msgspec/dict-based replacements for Marshmallow)
# --------------------------------------------------------------------------- #


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


# ---------- Field-level helpers ---------------------------------------------


def _err(field: str, msg: str, errors: dict[str, list[str]]) -> None:
    errors.setdefault(field, []).append(msg)


def _is_uuid(value: Any) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value))


def _check_string(
    value: Any,
    field: str,
    errors: dict[str, list[str]],
    *,
    required: bool = False,
    allow_none: bool = False,
    length: tuple[int, int] | None = None,
) -> None:
    if value is None:
        if required and not allow_none:
            _err(field, "Missing data for required field.", errors)
        return
    if not isinstance(value, str):
        _err(field, "Not a valid string.", errors)
        return
    if length is not None and not (length[0] <= len(value) <= length[1]):
        _err(
            field,
            f"Length must be between {length[0]} and {length[1]}.",
            errors,
        )


def _check_bool(
    value: Any,
    field: str,
    errors: dict[str, list[str]],
    *,
    allow_none: bool = False,
) -> None:
    if value is None:
        if not allow_none:
            _err(field, "Field may not be null.", errors)
        return
    if not isinstance(value, bool):
        _err(field, "Not a valid boolean.", errors)


def _check_int(
    value: Any,
    field: str,
    errors: dict[str, list[str]],
    *,
    allow_none: bool = False,
) -> None:
    if value is None:
        if not allow_none:
            _err(field, "Field may not be null.", errors)
        return
    if not isinstance(value, int) or isinstance(value, bool):
        _err(field, "Not a valid integer.", errors)


def _check_uuid(
    value: Any,
    field: str,
    errors: dict[str, list[str]],
    *,
    required: bool = True,
    allow_none: bool = False,
) -> None:
    if value is None:
        if required and not allow_none:
            _err(field, "Missing data for required field.", errors)
        return
    if not _is_uuid(str(value)):
        _err(field, "Not a valid UUID.", errors)


def _check_dict(
    value: Any,
    field: str,
    errors: dict[str, list[str]],
    *,
    allow_none: bool = False,
) -> None:
    if value is None:
        if not allow_none:
            _err(field, "Field may not be null.", errors)
        return
    if not isinstance(value, dict):
        _err(field, "Not a valid mapping.", errors)


def _raise_if_errors(file_label: str, errors: dict[str, list[str]]) -> None:
    if errors:
        raise CommandInvalidError({file_label: errors})


# ---------- Importv1*Schema ports -------------------------------------------


def database_schema(config: dict[str, Any]) -> None:
    """Port of ``ImportV1DatabaseSchema`` from ``superset_old/databases/schemas.py``.

    Includes the ``allow_file_upload`` -> ``allow_csv_upload`` pre_load rename.
    """
    # pre_load: ``allow_file_upload`` was renamed back from ``allow_csv_upload``
    # for backward compat with old V1 exports.
    if "allow_file_upload" in config:
        config["allow_csv_upload"] = config.pop("allow_file_upload")

    errors: dict[str, list[str]] = {}
    _check_string(config.get("database_name"), "database_name", errors, required=True)
    _check_string(config.get("sqlalchemy_uri"), "sqlalchemy_uri", errors, required=True)
    _check_string(config.get("password"), "password", errors, allow_none=True)
    _check_string(
        config.get("encrypted_extra"),
        "encrypted_extra",
        errors,
        allow_none=True,
    )
    _check_int(config.get("cache_timeout"), "cache_timeout", errors, allow_none=True)
    for fld in (
        "expose_in_sqllab",
        "allow_run_async",
        "allow_ctas",
        "allow_cvas",
        "allow_csv_upload",
        "impersonate_user",
    ):
        if fld in config:
            _check_bool(config.get(fld), fld, errors)
    if "allow_dml" in config:
        _check_bool(config.get("allow_dml"), "allow_dml", errors)
    if "extra" in config and config.get("extra") is not None:
        _check_dict(config.get("extra"), "extra", errors, allow_none=True)
    _check_uuid(config.get("uuid"), "uuid", errors, required=True)
    _check_string(config.get("version"), "version", errors, required=True)
    if "is_managed_externally" in config:
        _check_bool(
            config.get("is_managed_externally"),
            "is_managed_externally",
            errors,
            allow_none=True,
        )
    _check_string(config.get("external_url"), "external_url", errors, allow_none=True)
    if config.get("ssh_tunnel") is not None:
        _check_dict(config.get("ssh_tunnel"), "ssh_tunnel", errors, allow_none=True)
    _raise_if_errors("databases/", errors)


def column_schema(config: dict[str, Any]) -> None:
    """Port of ``ImportV1ColumnSchema``."""
    import json as _json

    if isinstance(config.get("extra"), str):
        try:
            config["extra"] = _json.loads(config["extra"])
        except (TypeError, _json.JSONDecodeError):
            pass

    errors: dict[str, list[str]] = {}
    _check_string(config.get("column_name"), "column_name", errors, required=True)
    if config.get("extra") is not None:
        _check_dict(config.get("extra"), "extra", errors, allow_none=True)
    _check_string(config.get("verbose_name"), "verbose_name", errors, allow_none=True)
    if "is_dttm" in config and config["is_dttm"] is not None:
        _check_bool(config["is_dttm"], "is_dttm", errors, allow_none=True)
    if "is_active" in config and config["is_active"] is not None:
        _check_bool(config["is_active"], "is_active", errors, allow_none=True)
    _check_string(config.get("type"), "type", errors, allow_none=True)
    _check_string(
        config.get("advanced_data_type"),
        "advanced_data_type",
        errors,
        allow_none=True,
    )
    if "groupby" in config:
        _check_bool(config.get("groupby"), "groupby", errors)
    if "filterable" in config:
        _check_bool(config.get("filterable"), "filterable", errors)
    _check_string(config.get("expression"), "expression", errors, allow_none=True)
    _check_string(config.get("description"), "description", errors, allow_none=True)
    _check_string(
        config.get("python_date_format"),
        "python_date_format",
        errors,
        allow_none=True,
    )
    if errors:
        raise CommandInvalidError(errors)


def metric_schema(config: dict[str, Any]) -> None:
    """Port of ``ImportV1MetricSchema``."""
    import json as _json

    if isinstance(config.get("extra"), str):
        try:
            config["extra"] = _json.loads(config["extra"])
        except (TypeError, _json.JSONDecodeError):
            pass
    if isinstance(config.get("currency"), str):
        try:
            config["currency"] = _json.loads(config["currency"])
        except (TypeError, _json.JSONDecodeError):
            pass

    errors: dict[str, list[str]] = {}
    _check_string(config.get("metric_name"), "metric_name", errors, required=True)
    _check_string(config.get("verbose_name"), "verbose_name", errors, allow_none=True)
    _check_string(config.get("metric_type"), "metric_type", errors, allow_none=True)
    _check_string(config.get("expression"), "expression", errors, required=True)
    _check_string(config.get("description"), "description", errors, allow_none=True)
    _check_string(config.get("d3format"), "d3format", errors, allow_none=True)
    if config.get("currency") is not None:
        _check_dict(config.get("currency"), "currency", errors, allow_none=True)
    if config.get("extra") is not None:
        _check_dict(config.get("extra"), "extra", errors, allow_none=True)
    _check_string(config.get("warning_text"), "warning_text", errors, allow_none=True)
    if errors:
        raise CommandInvalidError(errors)


def dataset_schema(config: dict[str, Any]) -> None:  # noqa: C901
    """Port of ``ImportV1DatasetSchema``."""
    import json as _json

    # pre_load fix_extra
    if isinstance(config.get("extra"), str):
        try:
            extra = config["extra"]
            config["extra"] = _json.loads(extra) if extra.strip() else None
        except ValueError:
            config["extra"] = None
    if config.get("template_params") == "":
        config["template_params"] = None

    errors: dict[str, list[str]] = {}
    _check_string(config.get("table_name"), "table_name", errors, required=True)
    _check_string(config.get("main_dttm_col"), "main_dttm_col", errors, allow_none=True)
    _check_string(config.get("description"), "description", errors, allow_none=True)
    _check_string(
        config.get("default_endpoint"),
        "default_endpoint",
        errors,
        allow_none=True,
    )
    if "offset" in config:
        _check_int(config.get("offset"), "offset", errors)
    _check_int(config.get("cache_timeout"), "cache_timeout", errors, allow_none=True)
    _check_string(config.get("schema"), "schema", errors, allow_none=True)
    _check_string(config.get("catalog"), "catalog", errors, allow_none=True)
    _check_string(config.get("sql"), "sql", errors, allow_none=True)
    if config.get("params") is not None:
        _check_dict(config.get("params"), "params", errors, allow_none=True)
    if config.get("template_params") is not None:
        _check_dict(
            config.get("template_params"), "template_params", errors, allow_none=True
        )
    if "filter_select_enabled" in config:
        _check_bool(
            config.get("filter_select_enabled"), "filter_select_enabled", errors
        )
    _check_string(
        config.get("fetch_values_predicate"),
        "fetch_values_predicate",
        errors,
        allow_none=True,
    )
    if config.get("extra") is not None:
        _check_dict(config.get("extra"), "extra", errors, allow_none=True)
    _check_uuid(config.get("uuid"), "uuid", errors, required=True)
    _check_string(config.get("version"), "version", errors, required=True)
    _check_uuid(config.get("database_uuid"), "database_uuid", errors, required=True)

    # ``columns`` and ``metrics`` are nested lists
    cols = config.get("columns") or []
    if not isinstance(cols, list):
        _err("columns", "Not a valid list.", errors)
    else:
        for idx, col in enumerate(cols):
            if not isinstance(col, dict):
                _err("columns", f"Index {idx}: Not a valid mapping.", errors)
                continue
            try:
                column_schema(col)
            except CommandInvalidError as ex:
                _err(
                    "columns",
                    f"Index {idx}: {ex.message if hasattr(ex, 'message') else ex}",
                    errors,
                )

    metrics = config.get("metrics") or []
    if not isinstance(metrics, list):
        _err("metrics", "Not a valid list.", errors)
    else:
        for idx, m in enumerate(metrics):
            if not isinstance(m, dict):
                _err("metrics", f"Index {idx}: Not a valid mapping.", errors)
                continue
            try:
                metric_schema(m)
            except CommandInvalidError as ex:
                _err(
                    "metrics",
                    f"Index {idx}: {ex.message if hasattr(ex, 'message') else ex}",
                    errors,
                )

    if "is_managed_externally" in config:
        _check_bool(
            config.get("is_managed_externally"),
            "is_managed_externally",
            errors,
            allow_none=True,
        )
    _check_string(config.get("external_url"), "external_url", errors, allow_none=True)
    if "normalize_columns" in config:
        _check_bool(config.get("normalize_columns"), "normalize_columns", errors)
    if "always_filter_main_dttm" in config:
        _check_bool(
            config.get("always_filter_main_dttm"),
            "always_filter_main_dttm",
            errors,
        )
    _raise_if_errors("datasets/", errors)


def chart_schema(config: dict[str, Any]) -> None:
    """Port of ``ImportV1ChartSchema``."""
    errors: dict[str, list[str]] = {}
    _check_string(config.get("slice_name"), "slice_name", errors, required=True)
    _check_string(config.get("description"), "description", errors, allow_none=True)
    _check_string(config.get("certified_by"), "certified_by", errors, allow_none=True)
    _check_string(
        config.get("certification_details"),
        "certification_details",
        errors,
        allow_none=True,
    )
    _check_string(config.get("viz_type"), "viz_type", errors, required=True)
    if "params" in config and config["params"] is not None:
        _check_dict(config.get("params"), "params", errors)
    _check_string(config.get("query_context"), "query_context", errors, allow_none=True)
    if config.get("query_context") is not None:
        # validates query_context as JSON
        import json as _json

        try:
            _json.loads(config["query_context"])
        except (TypeError, _json.JSONDecodeError):
            _err("query_context", "Not a valid JSON.", errors)
    _check_int(config.get("cache_timeout"), "cache_timeout", errors, allow_none=True)
    _check_uuid(config.get("uuid"), "uuid", errors, required=True)
    _check_string(config.get("version"), "version", errors, required=True)
    _check_uuid(config.get("dataset_uuid"), "dataset_uuid", errors, required=True)
    if "is_managed_externally" in config:
        _check_bool(
            config.get("is_managed_externally"),
            "is_managed_externally",
            errors,
            allow_none=True,
        )
    _check_string(config.get("external_url"), "external_url", errors, allow_none=True)
    tags = config.get("tags")
    if tags is not None:
        if not isinstance(tags, list):
            _err("tags", "Not a valid list.", errors)
        else:
            for idx, tag in enumerate(tags):
                if not isinstance(tag, str):
                    _err("tags", f"Index {idx}: Not a valid string.", errors)
    _raise_if_errors("charts/", errors)


def dashboard_schema(config: dict[str, Any]) -> None:  # noqa: C901  # complex business logic
    """Port of ``ImportV1DashboardSchema``."""
    errors: dict[str, list[str]] = {}
    _check_string(
        config.get("dashboard_title"), "dashboard_title", errors, required=True
    )
    _check_string(config.get("description"), "description", errors, allow_none=True)
    _check_string(config.get("css"), "css", errors, allow_none=True)
    _check_string(config.get("slug"), "slug", errors, allow_none=True)
    _check_uuid(config.get("uuid"), "uuid", errors, required=True)
    if "position" in config and config["position"] is not None:
        _check_dict(config.get("position"), "position", errors)
    if "metadata" in config and config["metadata"] is not None:
        _check_dict(config.get("metadata"), "metadata", errors)
    _check_string(config.get("version"), "version", errors, required=True)
    if "is_managed_externally" in config:
        _check_bool(
            config.get("is_managed_externally"),
            "is_managed_externally",
            errors,
            allow_none=True,
        )
    _check_string(config.get("external_url"), "external_url", errors, allow_none=True)
    _check_string(config.get("certified_by"), "certified_by", errors, allow_none=True)
    _check_string(
        config.get("certification_details"),
        "certification_details",
        errors,
        allow_none=True,
    )
    if "published" in config and config["published"] is not None:
        _check_bool(config.get("published"), "published", errors, allow_none=True)
    tags = config.get("tags")
    if tags is not None:
        if not isinstance(tags, list):
            _err("tags", "Not a valid list.", errors)
        else:
            for idx, tag in enumerate(tags):
                if not isinstance(tag, str):
                    _err("tags", f"Index {idx}: Not a valid string.", errors)
    if config.get("theme_uuid") is not None:
        _check_uuid(
            config.get("theme_uuid"),
            "theme_uuid",
            errors,
            required=False,
            allow_none=True,
        )
    if "theme_id" in config and config["theme_id"] is not None:
        _check_int(config.get("theme_id"), "theme_id", errors, allow_none=True)
    _raise_if_errors("dashboards/", errors)


def saved_query_schema(config: dict[str, Any]) -> None:
    """Port of ``ImportV1SavedQuerySchema``."""
    errors: dict[str, list[str]] = {}
    _check_string(
        config.get("catalog"),
        "catalog",
        errors,
        allow_none=True,
        length=(0, 128),
    )
    _check_string(
        config.get("schema"),
        "schema",
        errors,
        allow_none=True,
        length=(0, 128),
    )
    _check_string(
        config.get("label"),
        "label",
        errors,
        allow_none=True,
        length=(0, 256),
    )
    _check_string(config.get("description"), "description", errors, allow_none=True)
    _check_string(config.get("sql"), "sql", errors, required=True)
    _check_uuid(config.get("uuid"), "uuid", errors, required=True)
    _check_string(config.get("version"), "version", errors, required=True)
    _check_uuid(config.get("database_uuid"), "database_uuid", errors, required=True)
    _raise_if_errors("queries/", errors)


def theme_schema(config: dict[str, Any]) -> None:
    """Port of ``ImportV1ThemeSchema``."""
    errors: dict[str, list[str]] = {}
    _check_string(config.get("theme_name"), "theme_name", errors, required=True)
    if "json_data" not in config or config.get("json_data") is None:
        _err("json_data", "Missing data for required field.", errors)
    _check_uuid(config.get("uuid"), "uuid", errors, required=True)
    _check_string(config.get("version"), "version", errors, required=True)
    _raise_if_errors("themes/", errors)


def annotation_layer_schema(config: dict[str, Any]) -> None:
    """Validation for v1 annotation layer payloads.

    The original codebase doesn't ship a dedicated ``ImportV1AnnotationLayerSchema``;
    we provide the minimal field-set the import pipeline needs (name + uuid +
    version) so the bundle round-trips without crashing.
    """
    errors: dict[str, list[str]] = {}
    _check_string(config.get("name"), "name", errors, required=True)
    _check_uuid(config.get("uuid"), "uuid", errors, required=True)
    _check_string(config.get("version"), "version", errors, required=True)
    _raise_if_errors("annotation_layers/", errors)


# Mapping prefix -> schema callable, used by :class:`ImportAssetsCommand`.
# Matches the original ``superset_old/commands/importers/v1/assets.py``: only
# the five core asset types are part of the v1 asset bundle. Themes and
# annotation layers are imported via dedicated commands, not the asset bundle.
ASSET_SCHEMAS: dict[str, SchemaCallable] = {
    "databases/": database_schema,
    "datasets/": dataset_schema,
    "charts/": chart_schema,
    "dashboards/": dashboard_schema,
    "queries/": saved_query_schema,
}


# --------------------------------------------------------------------------- #
# Tag helper (async port)
# --------------------------------------------------------------------------- #


async def import_tag(  # noqa: C901
    target_tag_names: list[str],
    contents: dict[str, Any],
    object_id: int,
    object_type: str,
    session: AsyncSession,
) -> list[int]:
    """Async port of ``superset_old.commands.importers.v1.utils.import_tag``.

    Imports tags for charts and dashboards. Re-uses tag names if the row
    exists, otherwise creates a custom tag. Description is read from a
    ``tags.yaml`` entry in ``contents`` (matching the original).
    """
    # Feature flag — the original short-circuits if TAGGING_SYSTEM is off.
    try:
        from superset.utils.feature_flags import feature_flag_manager

        if not feature_flag_manager.is_feature_enabled("TAGGING_SYSTEM"):
            return []
    except Exception:  # noqa: BLE001
        return []

    try:
        from superset.models.tags import Tag, TaggedObject
    except ImportError:
        return []

    tag_descriptions: dict[str, str | None] = {}
    if "tags.yaml" in contents:
        try:
            tags_config = yaml.safe_load(contents["tags.yaml"]) or {}
        except yaml.YAMLError as err:
            logger.error("Error parsing tags.yaml: %s", err)
            tags_config = {}

        for tag_info in tags_config.get("tags", []):
            tag_name = tag_info.get("tag_name")
            description = tag_info.get("description", None)
            if tag_name:
                tag_descriptions[tag_name] = description

    existing_assocs = (
        (
            await session.execute(
                select(TaggedObject)
                .where(TaggedObject.object_id == object_id)
                .where(TaggedObject.object_type == object_type)
            )
        )
        .scalars()
        .all()
    )

    existing_tags_query = await session.execute(
        select(Tag).where(Tag.name.in_(target_tag_names))
    )
    existing_tags: dict[str, Any] = {
        str(tag.name): tag for tag in existing_tags_query.scalars().all()
    }

    new_tag_ids: list[int] = []
    for tag_name in target_tag_names:
        try:
            tag = existing_tags.get(tag_name)
            if tag is None:
                description = tag_descriptions.get(tag_name)
                tag = Tag(name=tag_name, description=description, type="custom")
                session.add(tag)
                await session.flush()
                existing_tags[tag_name] = tag

            tagged_object_q = await session.execute(
                select(TaggedObject)
                .where(TaggedObject.object_id == object_id)
                .where(TaggedObject.object_type == object_type)
                .where(TaggedObject.tag_id == tag.id)
            )
            existing_assoc = tagged_object_q.scalars().one_or_none()
            if existing_assoc is None:
                session.add(
                    TaggedObject(
                        tag_id=tag.id,
                        object_id=object_id,
                        object_type=object_type,
                    )
                )

            new_tag_ids.append(int(tag.id))
        except Exception as err:  # noqa: BLE001
            logger.error(
                "Error processing tag '%s' for %s ID %d: %s",
                tag_name,
                object_type,
                object_id,
                err,
            )
            continue

    # Remove old associations not in new set.
    for assoc in existing_assocs:
        if assoc.tag_id not in new_tag_ids:
            await session.delete(assoc)

    return new_tag_ids


__all__ = [
    "ASSET_SCHEMAS",
    "IMPORT_VERSION",
    "METADATA_FILE_NAME",
    "SchemaCallable",
    "annotation_layer_schema",
    "chart_schema",
    "column_schema",
    "dashboard_schema",
    "database_schema",
    "dataset_schema",
    "get_contents_from_bundle",
    "get_resource_mappings_batched",
    "import_tag",
    "is_valid_config",
    "load_configs",
    "load_metadata",
    "load_yaml",
    "metric_schema",
    "remove_root",
    "saved_query_schema",
    "theme_schema",
    "validate_metadata_type",
]
