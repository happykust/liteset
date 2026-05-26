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
"""Async import/export manager — coordinates bulk operations.

This module ports
``superset_old.commands.export.assets.ExportAssetsCommand`` and
``superset_old.commands.importers.v1.assets.ImportAssetsCommand`` into
two facades:

* :class:`AsyncFullAssetManager` — concrete implementation used by the
  public ``/api/v1/assets/`` endpoint.  ``export_assets()`` walks every
  asset type and aggregates the YAMLs produced by the per-resource
  export commands; ``import_assets()`` parses the bundle, then delegates
  to :class:`superset.commands.importers.v1.ImportAssetsCommand` for
  full dependency-aware orchestration.
* :class:`AsyncImportExportManager` — registry-style facade kept for
  backwards compatibility with code that registers individual
  per-resource export/import commands by resource type.

The per-asset-type registries (``_EXPORT_COMMANDS`` /
``_IMPORT_COMMANDS``) are populated on import via
:func:`register_default_importers` so callers don't need to wire them up
explicitly.  Each entry maps a bundle-prefix key (``"databases"``,
``"datasets"``, ...) to the corresponding ``Async*Command`` class.
"""

from __future__ import annotations

import asyncio
import io
import logging
import zipfile
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from superset.commands.importers.exceptions import (
    IncorrectFormatError,
    NoValidFilesFoundError,
)

logger = logging.getLogger(__name__)

MAX_ZIP_ENTRIES = 1000


# --------------------------------------------------------------------------- #
# Registry definitions
# --------------------------------------------------------------------------- #


# Asset types iterated for both export and import.  Order matters for export
# only as a presentational matter (the ZIP file's directory listing); imports
# are routed through :class:`ImportAssetsCommand` which enforces the
# database -> dataset -> chart -> dashboard dependency chain regardless of
# discovery order.
_ASSET_TYPES: tuple[str, ...] = (
    "databases",
    "datasets",
    "charts",
    "dashboards",
    "queries",
)

# Internal: maps asset-type prefix (e.g. ``"databases"``) -> tuple of
# ``(export_command_cls, import_command_cls, dao_factory)``.  The
# ``dao_factory`` is a callable taking an :class:`AsyncSession` and
# returning the DAO instance.  Populated by :func:`register_default_importers`.
_AssetEntry = dict[str, Any]
_REGISTRY: dict[str, _AssetEntry] = {}


def register_default_importers() -> None:
    """Populate :data:`_REGISTRY` with the built-in per-resource commands.

    Imports are deferred until first use to break the otherwise-circular
    dependency between this module and ``superset.commands.{database,…}``
    (the per-resource modules import :mod:`superset.importexport.export_base`
    and :mod:`.import_base`).
    """
    if _REGISTRY:
        return  # already initialised — idempotent

    from superset.commands.chart.export import ExportChartsCommand
    from superset.commands.chart.importers.v1 import ImportChartsCommand
    from superset.commands.dashboard.export import ExportDashboardsCommand
    from superset.commands.dashboard.importers.v1 import ImportDashboardsCommand
    from superset.commands.database.export import ExportDatabasesCommand
    from superset.commands.database.importers.v1 import ImportDatabasesCommand
    from superset.commands.dataset.export import ExportDatasetsCommand
    from superset.commands.dataset.importers.v1 import ImportDatasetsCommand
    from superset.commands.query.export import ExportSavedQueriesCommand
    from superset.commands.query.importers.v1 import ImportSavedQueriesCommand
    from superset.db.daos.chart import AsyncChartDAO
    from superset.db.daos.dashboard import AsyncDashboardDAO
    from superset.db.daos.database import AsyncDatabaseDAO
    from superset.db.daos.dataset import AsyncDatasetDAO
    from superset.db.daos.query import AsyncSavedQueryDAO

    _REGISTRY.update(
        {
            "databases": {
                "export_cls": ExportDatabasesCommand,
                "import_cls": ImportDatabasesCommand,
                "dao_factory": AsyncDatabaseDAO,
            },
            "datasets": {
                "export_cls": ExportDatasetsCommand,
                "import_cls": ImportDatasetsCommand,
                "dao_factory": AsyncDatasetDAO,
            },
            "charts": {
                "export_cls": ExportChartsCommand,
                "import_cls": ImportChartsCommand,
                "dao_factory": AsyncChartDAO,
            },
            "dashboards": {
                "export_cls": ExportDashboardsCommand,
                "import_cls": ImportDashboardsCommand,
                "dao_factory": AsyncDashboardDAO,
            },
            "queries": {
                "export_cls": ExportSavedQueriesCommand,
                "import_cls": ImportSavedQueriesCommand,
                "dao_factory": AsyncSavedQueryDAO,
            },
        }
    )


# --------------------------------------------------------------------------- #
# Result holder
# --------------------------------------------------------------------------- #


class ImportResult:
    """Result of an import operation."""

    def __init__(self) -> None:
        self.imported: dict[str, int] = {}
        self.errors: list[str] = []

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


# --------------------------------------------------------------------------- #
# Full-asset manager
# --------------------------------------------------------------------------- #


class AsyncFullAssetManager:
    """Full-asset import/export manager.

    Mirrors ``ExportAssetsCommand`` / ``ImportAssetsCommand`` from the
    original sync codebase: a single ZIP archive contains every asset
    type, each prefixed by its directory name, plus a ``metadata.yaml``
    manifest at the root.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        # Ensure the per-resource registry is populated before any
        # _export_type / _import_type call.
        register_default_importers()

    # ------------------------------------------------------------------ #
    # Export
    # ------------------------------------------------------------------ #

    async def export_assets(
        self,
        asset_types: list[str] | None = None,
    ) -> bytes:
        """Export every asset as a ZIP file.

        Returns the bytes of the archive — the controller streams them
        back to the client with ``Content-Type: application/zip``.
        """
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            metadata = {
                "version": "1.0.0",
                "type": "assets",
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            }
            zf.writestr("metadata.yaml", yaml.safe_dump(metadata, sort_keys=False))

            types_to_export = asset_types or list(_ASSET_TYPES)
            seen: set[str] = {"metadata.yaml"}

            for asset_type in types_to_export:
                try:
                    items = await self._export_type(asset_type)
                except Exception:  # noqa: BLE001
                    logger.warning("Failed to export %s", asset_type, exc_info=True)
                    continue

                for filename, content in items:
                    # Files come back already prefixed (e.g. ``databases/foo.yaml``)
                    # — the per-resource export commands handle that.  De-dupe
                    # on the full path so e.g. a database referenced by both a
                    # dashboard and a saved query isn't written twice.
                    if filename in seen:
                        continue
                    zf.writestr(filename, content)
                    seen.add(filename)

        return buf.getvalue()

    async def _export_type(self, asset_type: str) -> list[tuple[str, str]]:
        """Export every record of ``asset_type``.

        Returns a list of ``(file_path, yaml_content)`` tuples ready to be
        appended to a ZIP archive.  The per-resource export command's
        ``_export_single(model_id)`` method produces these tuples
        directly — we just enumerate every model id and concatenate.
        """
        entry = _REGISTRY.get(asset_type)
        if entry is None:
            logger.warning("No export command registered for: %s", asset_type)
            return []

        export_cls = entry["export_cls"]
        dao_factory = entry["dao_factory"]
        dao = dao_factory(self._session)

        # Fetch every id for this asset type via the DAO's underlying
        # model_cls.  We don't need ``find_all`` because we only want ids.
        model_cls = dao.model_cls
        ids = list((await self._session.execute(select(model_cls.id))).scalars().all())
        if not ids:
            return []

        cmd = export_cls(model_ids=ids, dao=dao)

        # ``AsyncExportModelsCommand._export_single`` is the per-id
        # generator we want — calling ``run()`` would re-build a
        # full ZIP with metadata, which is *not* what we want here.
        results: list[tuple[str, str]] = []
        for model_id in ids:
            try:
                results.extend(await cmd._export_single(model_id))  # noqa: SLF001
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to export %s id=%s", asset_type, model_id, exc_info=True
                )
        return results

    # ------------------------------------------------------------------ #
    # Import
    # ------------------------------------------------------------------ #

    async def import_assets(
        self,
        contents: dict[str, str] | None = None,
        overwrite: bool = False,
        passwords: dict[str, str] | None = None,
        ssh_tunnel_passwords: dict[str, str] | None = None,
        ssh_tunnel_private_keys: dict[str, str] | None = None,
        ssh_tunnel_private_key_passwords: dict[str, str] | None = None,
        sparse: bool = False,
        security_manager: Any | None = None,
        current_user: Any | None = None,
        file_content: bytes | None = None,
    ) -> ImportResult:
        """Import assets from a parsed bundle.

        Mirrors the original Flask path
        (``superset_old/importexport/api.py:import_``): the controller parses
        the upload via ``get_contents_from_bundle`` (which applies
        ``remove_root`` so a nested ``assets_export_<ts>/`` bundle is
        flattened) and passes the resulting ``{file_name: text}`` mapping
        straight to :class:`ImportAssetsCommand`.

        Args:
            contents: Canonical ``{file_name: text-content}`` mapping
                produced by ``get_contents_from_bundle`` — the same shape
                the original ``ImportAssetsCommand`` consumed.
            overwrite: Whether to overwrite existing assets.
            passwords: Database connection passwords keyed by **file name**
                (e.g. ``{"databases/MyDatabase.yaml": "pw"}``), matching the
                original Marshmallow contract; UUID fallback against existing
                DB rows happens inside ``load_configs``.
            ssh_tunnel_passwords: SSH tunnel passwords keyed by file name.
            ssh_tunnel_private_keys: SSH tunnel private keys keyed by file name.
            ssh_tunnel_private_key_passwords: SSH tunnel private key
                passphrases keyed by file name.
            sparse: If ``True`` only update fields present in the import
                file, preserving other existing fields on the target
                model.  Mirrors the original ``ImportAssetsCommand(sparse=True)``.
            security_manager: Optional :class:`AsyncSecurityManager` for
                permission checks.  Tests / CLI imports may omit it.
            current_user: Optional :class:`User` model for owner attribution.
            file_content: Backwards-compat — raw ZIP bytes.  When supplied
                (and ``contents`` is ``None``) the manager parses the bundle
                itself via :meth:`_parse_import_zip` (which now applies
                ``remove_root`` to strip the root export folder).

        Validation/import failures are NOT swallowed: ``CommandException``
        and its subclasses (``IncorrectFormatError`` / ``NoValidFilesFoundError``
        / ``CommandInvalidError``) propagate to the controller so the global
        exception handler maps them to the matching 4xx — matching the
        original FAB behaviour rather than wrapping everything in a 200.
        """
        result = ImportResult()
        by_type: dict[str, list[str]] = {}

        if contents is None:
            if file_content is None:
                raise NoValidFilesFoundError()
            # Backwards-compat: parse the raw bytes ourselves.  ``BadZipFile``
            # surfaces as ``IncorrectFormatError`` (422) rather than being
            # swallowed into a 200 ``result.errors``.
            try:
                metadata, by_type, contents = await asyncio.to_thread(
                    self._parse_import_zip, file_content
                )
            except zipfile.BadZipFile as exc:
                raise IncorrectFormatError("Not a ZIP file") from exc
            if metadata is not None:
                logger.info("Importing assets version %s", metadata.get("version"))

        if not contents:
            raise NoValidFilesFoundError()

        # Build per-type counters for the response payload from the parsed
        # ``contents`` when the controller pre-parsed the bundle for us.
        if not by_type:
            by_type = _count_by_type(contents)

        # ------------------------------------------------------------------
        # Delegate to ImportAssetsCommand for dependency-aware orchestration.
        # This matches the original Flask path which routed every full-bundle
        # import through ``ImportAssetsCommand`` rather than running each
        # per-type command in isolation (which would lose ordering).
        # ------------------------------------------------------------------
        from superset.commands.importers.v1 import ImportAssetsCommand

        cmd = ImportAssetsCommand(
            contents=contents,
            session=self._session,
            passwords=passwords,
            ssh_tunnel_passwords=ssh_tunnel_passwords,
            ssh_tunnel_private_keys=ssh_tunnel_private_keys,
            ssh_tunnel_priv_key_passwords=ssh_tunnel_private_key_passwords,
            sparse=sparse,
            security_manager=security_manager,
            current_user=current_user,
        )

        # Let CommandException (and subclasses) propagate so the controller
        # surfaces a 4xx — do NOT trap it into ``result.errors`` (HTTP 200).
        await cmd.execute()

        # Populate per-type counts for the response payload — the
        # controller surfaces these so the frontend can show "Imported
        # 3 dashboards, 7 charts, ...".
        for asset_type, filenames in by_type.items():
            result.imported[asset_type] = len(filenames)

        return result

    @staticmethod
    def _parse_import_zip(
        file_content: bytes,
    ) -> tuple[
        dict[str, Any] | None,
        dict[str, list[str]],
        dict[str, str],
    ]:
        """Parse and validate a ZIP file synchronously (run via to_thread).

        Returns ``(metadata, by_type, contents)`` where:

        * ``metadata`` is the parsed ``metadata.yaml`` dict (or ``None``
          if absent).
        * ``by_type`` maps asset-type prefix -> list of file names that
          live under that prefix (used for per-type counters).
        * ``contents`` is the canonical ``{file_name: text-content}``
          mapping consumed by :class:`ImportAssetsCommand`.

        Mirrors ``get_contents_from_bundle`` from the original
        ``superset_old/commands/importers/v1/utils.py``: every entry is
        filtered through :func:`is_valid_config` (drop dotfiles /
        underscore-prefixed / non-YAML files) and stripped of its leading
        export folder via :func:`remove_root`.  Upstream exports always nest
        everything under ``assets_export_<ts>/``; without ``remove_root`` the
        ``metadata.yaml`` lookup misses and zero objects import.

        Raises :class:`ValueError` on entry count or path traversal
        violations; :class:`zipfile.BadZipFile` propagates upwards from
        :class:`zipfile.ZipFile` itself.
        """
        from superset.commands.importers.v1.utils import is_valid_config, remove_root

        zf = zipfile.ZipFile(io.BytesIO(file_content))
        try:
            entries = [n for n in zf.namelist() if not n.endswith("/")]

            if len(entries) > MAX_ZIP_ENTRIES:
                raise ValueError(
                    f"ZIP contains too many entries "
                    f"({len(entries)} > {MAX_ZIP_ENTRIES})"
                )

            for entry in entries:
                parts = PurePosixPath(entry).parts
                if ".." in parts:
                    raise ValueError(f"ZIP entry contains path traversal: {entry}")

            # Apply remove_root + is_valid_config exactly like the original
            # ``get_contents_from_bundle`` so a nested bundle flattens to the
            # canonical ``metadata.yaml`` / ``databases/...`` layout.
            contents: dict[str, str] = {}
            for entry in entries:
                if not is_valid_config(entry):
                    continue
                contents[remove_root(entry)] = zf.read(entry).decode("utf-8")

            metadata: dict[str, Any] | None = None
            if "metadata.yaml" in contents:
                metadata = yaml.safe_load(contents["metadata.yaml"])

            by_type: dict[str, list[str]] = {}
            for file_name in contents:
                if file_name == "metadata.yaml":
                    continue
                # Only the first path component is the type prefix.
                head = file_name.split("/", 1)[0]
                # Skip anything that isn't a known asset directory; this
                # keeps stray top-level files out of the counters but keeps
                # them in ``contents`` so the caller can still see them.
                if head in _ASSET_TYPES:
                    by_type.setdefault(head, []).append(file_name)

            return metadata, by_type, contents
        finally:
            zf.close()

    async def _import_type(
        self,
        contents: dict[str, str],
        asset_type: str,
        filenames: list[str],
        overwrite: bool,
        passwords: dict[str, str] | None,
    ) -> int:
        """Import items of a single ``asset_type`` from an already-parsed bundle.

        Builds a per-type ZIP wrapper around the relevant ``filenames``
        and feeds it to the registered per-resource ``Import*Command``.
        The asset-type-specific command is responsible for parsing the
        YAMLs, resolving dependencies (e.g. dashboards bring along
        charts/datasets/databases), and persisting via the appropriate
        DAO.

        Returns the count of files attempted (matching the original's
        contract — the per-resource command may itself decide to skip
        rows on UUID dedup).
        """
        entry = _REGISTRY.get(asset_type)
        if entry is None:
            logger.warning("No import command registered for: %s", asset_type)
            return 0

        import_cls = entry["import_cls"]
        dao_factory = entry["dao_factory"]
        dao = dao_factory(self._session)

        # Build a minimal ZIP archive containing only this asset type's
        # files (plus its dependencies that already live in ``contents``)
        # plus a metadata.yaml stamped with the right type — that's what
        # the per-resource import command expects.
        buf = io.BytesIO()
        type_singular = _type_singular(asset_type)
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "metadata.yaml",
                yaml.safe_dump(
                    {
                        "version": "1.0.0",
                        "type": type_singular,
                        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                    },
                    sort_keys=False,
                ),
            )
            for name in filenames:
                if name in contents:
                    zf.writestr(name, contents[name])
            # Also include dependent files (databases needed by datasets,
            # datasets needed by charts/dashboards) to mirror the
            # original importer's behaviour.
            for dep_prefix in _dependencies_for(asset_type):
                for fname, payload in contents.items():
                    if fname.startswith(dep_prefix + "/") and fname not in filenames:
                        zf.writestr(fname, payload)
        buf.seek(0)

        cmd = import_cls(
            contents=buf,
            dao=dao,
            overwrite=overwrite,
            passwords=passwords,
        )
        await cmd.execute()
        return len(filenames)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _count_by_type(contents: dict[str, str]) -> dict[str, list[str]]:
    """Group bundle file names by their asset-type prefix.

    Stray top-level / unknown-prefix files are ignored (kept out of the
    per-type counters) but remain in ``contents`` for the importer.
    """
    by_type: dict[str, list[str]] = {}
    for file_name in contents:
        if file_name == "metadata.yaml":
            continue
        head = file_name.split("/", 1)[0]
        if head in _ASSET_TYPES:
            by_type.setdefault(head, []).append(file_name)
    return by_type


def _type_singular(asset_type: str) -> str:
    """Convert ``"databases"`` -> ``"Database"``.

    Mirrors the metadata ``type`` field the per-resource importers
    validate (``Database``, ``Slice``, ``Dashboard``, ``SqlaTable``,
    ``SavedQuery``) — see ``AsyncImportModelsCommand._expected_type``.
    """
    return {
        "databases": "Database",
        "datasets": "SqlaTable",
        "charts": "Slice",
        "dashboards": "Dashboard",
        "queries": "SavedQuery",
    }.get(asset_type, asset_type)


def _dependencies_for(asset_type: str) -> tuple[str, ...]:
    """Return the asset-type prefixes that ``asset_type`` depends on."""
    return {
        "databases": (),
        "queries": ("databases",),
        "datasets": ("databases",),
        "charts": ("databases", "datasets"),
        "dashboards": ("databases", "datasets", "charts"),
    }.get(asset_type, ())


# --------------------------------------------------------------------------- #
# Registry-style facade (kept for backwards-compat with controllers that
# expect register_export / register_import / export / import_models).
# --------------------------------------------------------------------------- #


class AsyncImportExportManager:
    """Facade for export/import operations keyed by resource type.

    Used by callers that need to invoke a single per-resource command
    rather than the full-bundle path.  The ``register_*`` methods let
    plugins or test suites register additional asset types at runtime.
    """

    _EXPORT_COMMANDS: dict[str, type] = {}
    _IMPORT_COMMANDS: dict[str, type] = {}

    @classmethod
    def register_export(cls, resource_type: str, command_cls: type) -> None:
        cls._EXPORT_COMMANDS[resource_type] = command_cls

    @classmethod
    def register_import(cls, resource_type: str, command_cls: type) -> None:
        cls._IMPORT_COMMANDS[resource_type] = command_cls

    @classmethod
    def _ensure_registered(cls) -> None:
        """Lazy-populate from the module-level ``_REGISTRY``."""
        if cls._EXPORT_COMMANDS and cls._IMPORT_COMMANDS:
            return
        register_default_importers()
        for asset_type, entry in _REGISTRY.items():
            cls._EXPORT_COMMANDS.setdefault(asset_type, entry["export_cls"])
            cls._IMPORT_COMMANDS.setdefault(asset_type, entry["import_cls"])

    @classmethod
    async def export(cls, resource_type: str, model_ids: list[int]) -> io.BytesIO:
        cls._ensure_registered()
        if resource_type not in cls._EXPORT_COMMANDS:
            raise ValueError(f"No export command registered for: {resource_type}")
        cmd_cls = cls._EXPORT_COMMANDS[resource_type]
        cmd = cmd_cls(model_ids=model_ids)
        return await cmd.execute()

    @classmethod
    async def import_models(
        cls,
        resource_type: str,
        contents: io.BytesIO,
        **kwargs: Any,
    ) -> None:
        cls._ensure_registered()
        if resource_type not in cls._IMPORT_COMMANDS:
            raise ValueError(f"No import command registered for: {resource_type}")
        cmd_cls = cls._IMPORT_COMMANDS[resource_type]
        cmd = cmd_cls(contents=contents, **kwargs)
        await cmd.execute()


# --------------------------------------------------------------------------- #
# Compatibility alias
# --------------------------------------------------------------------------- #


# Some callers refer to the manager as ``BundleManager`` (the canonical
# name used in the design doc); expose both for parity.
BundleManager = AsyncFullAssetManager


__all__ = [
    "AsyncFullAssetManager",
    "AsyncImportExportManager",
    "BundleManager",
    "ImportResult",
    "MAX_ZIP_ENTRIES",
    "register_default_importers",
]
