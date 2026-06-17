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
"""Base async import command — reads YAML+ZIP bundles."""

from __future__ import annotations

import asyncio
import io
import logging
import zipfile
from abc import abstractmethod
from pathlib import PurePosixPath
from typing import Any

import yaml

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError

logger = logging.getLogger(__name__)

MAX_ZIP_ENTRIES = 1000
MAX_ENTRY_SIZE = 50 * 1024 * 1024  # 50 MB


class AsyncImportModelsCommand(AsyncBaseCommand[None]):
    """Import models from a ZIP file containing YAML manifests."""

    _expected_type: str = ""  # Override in subclasses to validate metadata type

    def __init__(
        self,
        contents: io.BytesIO,
        passwords: dict[str, str] | None = None,
        ssh_tunnel_passwords: dict[str, str] | None = None,
        ssh_tunnel_private_keys: dict[str, str] | None = None,
        ssh_tunnel_private_key_passwords: dict[str, str] | None = None,
        overwrite: bool = False,
    ) -> None:
        self._contents = contents
        self._passwords = passwords or {}
        self._ssh_tunnel_passwords = ssh_tunnel_passwords or {}
        self._ssh_tunnel_private_keys = ssh_tunnel_private_keys or {}
        self._ssh_tunnel_private_key_passwords = ssh_tunnel_private_key_passwords or {}
        self._overwrite = overwrite
        self._configs: dict[str, dict[str, Any]] | None = None
        self._db_passwords: dict[str, str] = {}
        self._db_ssh_tunnel_passwords: dict[str, str] = {}
        self._db_ssh_tunnel_private_keys: dict[str, str] = {}
        self._db_ssh_tunnel_priv_key_passws: dict[str, str] = {}

    @staticmethod
    def _zip_max_compress_ratio() -> float:
        """``ZIP_FILE_MAX_COMPRESS_RATIO`` (default 200×) — best-effort lazy."""
        try:
            from superset.config import SupersetSettings

            return float(
                getattr(
                    SupersetSettings(),  # type: ignore[call-arg]
                    "zip_file_max_compress_ratio",
                    200.0,
                )
            )
        except Exception:  # noqa: BLE001
            return 200.0

    def _parse_zip(self) -> dict[str, dict[str, Any]]:
        """Parse ZIP file into ``{filename: parsed_yaml_dict}``.

        * Skip system files (names starting with ``.`` or ``_``) and any
          entry that isn't a ``.yaml`` / ``.yml`` file — the original
          ``is_valid_config`` rule.
        * Strip the top-level export directory from every path
          (``dashboard_export_20240101T123000/metadata.yaml`` →
          ``metadata.yaml``), matching ``remove_root``.  Without this
          ``self._configs.get("metadata.yaml")`` always misses and we
          return the bogus "Missing metadata.yaml" error even though the
          bundle contains it under the export folder.
        * Raises ``CommandInvalidError`` (422) for malformed uploads
          instead of leaking ``zipfile.BadZipFile`` as 500 — matches
          upstream's ``IncorrectFormatError`` for the same condition
          (``charts/api.py::import_``: ``if not is_zipfile(upload):
          raise IncorrectFormatError("Not a ZIP file")``).
        """
        try:
            zf_ctx = zipfile.ZipFile(self._contents)
        except zipfile.BadZipFile as ex:
            raise CommandInvalidError(
                "Uploaded file is not a valid ZIP archive."
            ) from ex
        configs: dict[str, dict[str, Any]] = {}
        with zf_ctx as zf:
            # Zip-bomb guard — the missing half of upstream's
            # ``check_is_safe_zip``: reject the archive when total
            # uncompressed/compressed exceeds ``ZIP_FILE_MAX_COMPRESS_RATIO``
            # (default 200x) BEFORE any ``zf.read`` decompresses an entry into
            # memory. Inspects ``infolist()`` metadata only. The entry-count and
            # path-traversal handling below remain the port's existing behavior
            # (so we add only the ratio check rather than calling the full
            # ``_check_is_safe_zip``, whose count/traversal semantics differ).
            infos = zf.infolist()
            total_uncompressed = sum(zi.file_size for zi in infos)
            total_compressed = sum(zi.compress_size for zi in infos)
            max_ratio = self._zip_max_compress_ratio()
            if total_compressed and total_uncompressed / total_compressed > max_ratio:
                raise CommandInvalidError("Zip compress ratio above allowed threshold.")
            entries = [n for n in zf.namelist() if not n.endswith("/")]
            if len(entries) > MAX_ZIP_ENTRIES:
                raise ValueError(
                    f"ZIP contains too many entries "
                    f"({len(entries)} > {MAX_ZIP_ENTRIES})"
                )
            for name in entries:
                # Path-traversal guard first — reject any segment that
                # tries to escape the bundle root.  Done before the
                # ``remove_root`` strip so a malicious "../etc/passwd"
                # entry can't slip through by being placed at the top.
                parts = PurePosixPath(name).parts
                safe_parts = tuple(p for p in parts if p not in ("..", "/"))
                if not safe_parts:
                    continue
                # ``is_valid_config``: skip dotfiles / underscore-prefixed
                # entries and anything that isn't YAML.
                leaf = PurePosixPath(*safe_parts).name
                if leaf.startswith(".") or leaf.startswith("_"):
                    continue
                if PurePosixPath(leaf).suffix.lower() not in (".yaml", ".yml"):
                    continue
                # ``remove_root``: drop the first segment (the export
                # bundle directory).  When the entry was already at the
                # top level the result becomes empty — fall back to the
                # leaf name to keep ``metadata.yaml`` reachable either
                # way.
                if len(safe_parts) > 1:
                    rel_path = str(PurePosixPath(*safe_parts[1:]))
                else:
                    rel_path = safe_parts[0]
                raw = zf.read(name)
                if len(raw) > MAX_ENTRY_SIZE:
                    raise ValueError(
                        f"ZIP entry '{name}' too large "
                        f"({len(raw)} > {MAX_ENTRY_SIZE} bytes)"
                    )
                configs[rel_path] = yaml.safe_load(raw)
        return configs

    async def validate(self) -> None:
        self._contents.seek(0)
        self._configs = await asyncio.to_thread(self._parse_zip)

        metadata = self._configs.get("metadata.yaml")
        if metadata is None:
            raise CommandInvalidError("Missing metadata.yaml in import bundle")
        version = metadata.get("version") if isinstance(metadata, dict) else None
        if version != "1.0.0":
            raise CommandInvalidError(f"Unsupported import version: {version}")
        if self._expected_type and isinstance(metadata, dict):
            meta_type = metadata.get("type")
            if meta_type and meta_type != self._expected_type:
                raise CommandInvalidError(
                    f"Expected type '{self._expected_type}', got '{meta_type}'"
                )

        await self._load_existing_secrets()

        validatable = {k: v for k, v in self._configs.items() if k != "metadata.yaml"}

        # NOTE: UUID-existence checking is NOT done here.  The orchestrated
        # import commands (ImportChartsCommand, ImportDashboardsCommand) handle
        # dedup themselves because dependencies (databases, datasets) should be
        # reused when they already exist, while only the primary resource type
        # respects the user's overwrite flag.  Subclasses that need a blanket
        # pre-check can implement it in their _validate() override.
        for file_name, content in validatable.items():
            if isinstance(content, dict):
                self._apply_password(content, file_name)

        self._validate_entry_schemas(validatable)

        await self._validate(validatable)

    def _validate_entry_schemas(self, validatable: dict[str, dict[str, Any]]) -> None:
        """Validate each recognized bundle entry against its prefix schema.

        Mirrors ``load_configs``' per-entry ``schema(config)`` call using the
        shared :data:`ASSET_SCHEMAS` registry. Validation runs on a deep copy
        so a validator's normalisation (e.g. ``database_schema``'s
        ``allow_file_upload`` rename) never mutates the config that the import
        path later consumes.
        """
        import copy

        from superset.commands.importers.v1.utils import ASSET_SCHEMAS

        errors: dict[str, Any] = {}
        for file_name, content in validatable.items():
            if not isinstance(content, dict):
                continue
            prefix = f"{file_name.split('/')[0]}/"
            schema = ASSET_SCHEMAS.get(prefix)
            if schema is None:
                continue
            try:
                schema(copy.deepcopy(content))
            except CommandInvalidError as exc:
                # Validators raise ``CommandInvalidError({prefix: {field: [...]}})``;
                # the structured dict lives in ``extra["errors"]`` (CommandException
                # stringifies ``message``). Re-key by the actual file name so the
                # 422 payload matches the original Marshmallow shape.
                structured = getattr(exc, "extra", {}).get("errors")
                if isinstance(structured, dict):
                    errors[file_name] = structured.get(prefix, structured)
                else:
                    errors[file_name] = getattr(exc, "message", None) or str(exc)

        if errors:
            raise CommandInvalidError(errors)

    async def run(self) -> None:
        if self._configs is None:
            raise CommandInvalidError("validate() must be called before run()")
        configs = self._configs
        for file_name, content in configs.items():
            if file_name == "metadata.yaml":
                continue
            if isinstance(content, dict):
                content = self._apply_password(content, file_name)
            await self._import_single(file_name, content)

    async def _load_existing_secrets(self) -> None:
        """Cache existing DB / SSH-tunnel secrets keyed by UUID.

        Provides the UUID fallback when the request didn't pass a
        file-name-keyed secret.  Best-effort: subclasses without a DAO/session
        leave the caches empty (the file-name keying still works).
        """
        dao = getattr(self, "_dao", None)
        session = getattr(dao, "session", None) if dao is not None else None
        if session is None:
            return
        try:
            from superset.commands.importers.v1.utils import (
                _existing_database_secrets,
            )

            (
                self._db_passwords,
                self._db_ssh_tunnel_passwords,
                self._db_ssh_tunnel_private_keys,
                self._db_ssh_tunnel_priv_key_passws,
            ) = await _existing_database_secrets(session)
        except Exception:  # noqa: BLE001
            logger.debug("Could not preload existing DB secrets", exc_info=True)

    def _apply_password(
        self,
        content: dict[str, Any],
        file_name: str | None = None,
    ) -> dict[str, Any]:
        """Splice in masked passwords / SSH-tunnel secrets.

        Each secret is matched **by file name first**
        (``{"databases/MyDatabase.yaml": "pw"}``), then falls back to the
        secret of an existing database with the same UUID.  The value is
        written to ``config["password"]`` / ``config["ssh_tunnel"][...]``
        (NOT spliced into the ``sqlalchemy_uri`` string) — the database
        importer later masks it into the URI via ``set_sqlalchemy_uri``.
        """
        uuid = content.get("uuid", "")
        is_database = bool(file_name) and str(file_name).startswith("databases/")

        if file_name is not None and file_name in self._passwords:
            content["password"] = self._passwords[file_name]
        elif is_database and uuid and uuid in self._db_passwords:
            content["password"] = self._db_passwords[uuid]

        if file_name is not None and file_name in self._ssh_tunnel_passwords:
            content.setdefault("ssh_tunnel", {})
            content["ssh_tunnel"]["password"] = self._ssh_tunnel_passwords[file_name]
        elif (
            is_database
            and uuid
            and uuid in self._db_ssh_tunnel_passwords
            and content.get("ssh_tunnel") is not None
        ):
            content["ssh_tunnel"]["password"] = self._db_ssh_tunnel_passwords[uuid]

        if file_name is not None and file_name in self._ssh_tunnel_private_keys:
            content.setdefault("ssh_tunnel", {})
            content["ssh_tunnel"]["private_key"] = self._ssh_tunnel_private_keys[
                file_name
            ]
        elif (
            is_database
            and uuid
            and uuid in self._db_ssh_tunnel_private_keys
            and content.get("ssh_tunnel") is not None
        ):
            content["ssh_tunnel"]["private_key"] = self._db_ssh_tunnel_private_keys[
                uuid
            ]

        priv_key_pw = self._ssh_tunnel_private_key_passwords
        if file_name is not None and file_name in priv_key_pw:
            content.setdefault("ssh_tunnel", {})
            content["ssh_tunnel"]["private_key_password"] = priv_key_pw[file_name]
        elif (
            is_database
            and uuid
            and uuid in self._db_ssh_tunnel_priv_key_passws
            and content.get("ssh_tunnel") is not None
        ):
            content["ssh_tunnel"]["private_key_password"] = (
                self._db_ssh_tunnel_priv_key_passws[uuid]
            )

        return content

    @abstractmethod
    async def _import_single(self, file_name: str, content: dict[str, Any]) -> None: ...

    @abstractmethod
    async def _validate(self, configs: dict[str, dict[str, Any]]) -> None: ...

    async def _check_existing(self, uuid_val: str) -> bool:
        """Check if an object with this UUID already exists.

        Subclasses should override with UUID-based DB lookup.
        Default implementation returns False (no check).
        """
        return False
