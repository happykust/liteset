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
"""Tests for AsyncFullAssetManager."""

from __future__ import annotations

import io
import zipfile
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from superset.importexport import manager as manager_module
from superset.importexport.manager import AsyncFullAssetManager, ImportResult


@pytest.fixture
def mock_session() -> AsyncMock:
    return AsyncMock()


class _FakeExportCommand:
    """Stand-in for the registered export command — yields fixture tuples."""

    def __init__(self, asset_type: str, ids: list[int]) -> None:
        self._asset_type = asset_type
        self._ids = ids

    async def _export_single(self, model_id: int) -> list[tuple[str, str]]:
        return [
            (
                f"{self._asset_type}/item_{model_id}.yaml",
                yaml.safe_dump({"id": model_id}),
            )
        ]


class _FakeImportCommand:
    """Stand-in for the registered import command — records the call."""

    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_kwargs = kwargs

    async def run(self) -> None:
        return None


def _patch_registry(asset_type: str, ids: list[int]) -> dict[str, Any]:
    """Build a one-entry registry for ``asset_type`` mocking export+import."""

    class _DAOFactory:
        def __init__(self, session: Any) -> None:
            self._session = session

        async def find_all_ids(self) -> list[int]:
            return ids

    return {
        asset_type: {
            "export_cls": lambda dao: _FakeExportCommand(asset_type, ids),
            "import_cls": _FakeImportCommand,
            "dao_factory": _DAOFactory,
        }
    }


@pytest.fixture
def manager(mock_session: AsyncMock) -> AsyncFullAssetManager:
    return AsyncFullAssetManager(mock_session)


async def test_export_creates_valid_zip(manager: AsyncFullAssetManager) -> None:
    """export_assets returns bytes that form a valid ZIP with metadata."""
    with patch.object(manager_module, "_REGISTRY", _patch_registry("charts", [])):
        data = await manager.export_assets(asset_types=["charts"])
    assert isinstance(data, bytes)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        # Entries are nested under ``assets_export_<ts>/`` so the bundle
        # round-trips through the importer's unconditional ``remove_root``.
        assert len(names) == 1
        meta_name = names[0]
        assert meta_name.startswith("assets_export_")
        assert meta_name.endswith("/metadata.yaml")
        metadata = yaml.safe_load(zf.read(meta_name))
        assert metadata["version"] == "1.0.0"
        assert metadata["type"] == "assets"
        assert "timestamp" in metadata


async def test_export_writes_per_resource_entries(
    manager: AsyncFullAssetManager,
) -> None:
    """Per-resource export tuples are written into the ZIP under their
    asset-type directory; the manager dedupes on full path."""

    async def fake_export_type(asset_type: str) -> list[tuple[str, str]]:
        if asset_type == "charts":
            return [
                ("charts/item_1.yaml", yaml.safe_dump({"id": 1})),
                ("charts/item_2.yaml", yaml.safe_dump({"id": 2})),
            ]
        return []

    with patch.object(manager, "_export_type", side_effect=fake_export_type):
        data = await manager.export_assets(asset_types=["charts"])

    from superset.commands.importers.v1.utils import remove_root

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = sorted(zf.namelist())
        # Every entry is nested under a single ``assets_export_<ts>/`` root;
        # after stripping it (as the importer does) the canonical layout is
        # restored, making the bundle round-trip-importable.
        roots = {name.split("/", 1)[0] for name in names}
        assert len(roots) == 1
        assert next(iter(roots)).startswith("assets_export_")
        stripped = sorted(remove_root(name) for name in names)
        assert stripped == ["charts/item_1.yaml", "charts/item_2.yaml", "metadata.yaml"]
        item_name = next(n for n in names if n.endswith("charts/item_1.yaml"))
        item = yaml.safe_load(zf.read(item_name))
        assert item == {"id": 1}


async def test_export_unknown_asset_type_skipped(
    manager: AsyncFullAssetManager,
) -> None:
    """Unknown asset types log a warning and produce no entries (no exception)."""
    with patch.object(manager_module, "_REGISTRY", {}):
        data = await manager.export_assets(asset_types=["does_not_exist"])
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        # Only the (nested) metadata entry is written for an unknown type.
        assert len(names) == 1
        assert names[0].startswith("assets_export_")
        assert names[0].endswith("/metadata.yaml")


async def test_import_handles_bad_zip(manager: AsyncFullAssetManager) -> None:
    """import_assets raises IncorrectFormatError (422) for invalid ZIP content.

    Matches the original FAB behaviour (``IncorrectFormatError`` → 4xx) rather
    than swallowing the failure into a 200 ``result.errors``.
    """
    from superset.commands.importers.exceptions import IncorrectFormatError

    with pytest.raises(IncorrectFormatError):
        await manager.import_assets(file_content=b"not a zip file")


async def test_import_invokes_assets_command(
    manager: AsyncFullAssetManager,
) -> None:
    """Valid bundle dispatches to ``ImportAssetsCommand`` and per-type counts
    are populated from the parsed file groups."""
    # Upstream export bundles nest everything under an ``assets_export_<ts>/``
    # root that ``get_contents_from_bundle`` strips via ``remove_root``.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "assets_export_test/metadata.yaml",
            yaml.safe_dump({"version": "1.0.0", "type": "assets"}),
        )
        zf.writestr(
            "assets_export_test/charts/c1.yaml",
            yaml.safe_dump({"slug": "c1", "uuid": "u-1"}),
        )
        zf.writestr(
            "assets_export_test/charts/c2.yaml",
            yaml.safe_dump({"slug": "c2", "uuid": "u-2"}),
        )

    captured_kwargs: dict[str, Any] = {}

    class _StubAssetsCommand:
        def __init__(self, **kwargs: Any) -> None:
            captured_kwargs.update(kwargs)

        async def execute(self) -> None:
            return None

    with patch(
        "superset.commands.importers.v1.ImportAssetsCommand", _StubAssetsCommand
    ):
        result = await manager.import_assets(file_content=buf.getvalue())

    assert result.success, result.errors
    assert captured_kwargs.get("contents"), "ImportAssetsCommand received no contents"
    assert "charts/c1.yaml" in captured_kwargs["contents"]
    assert result.imported.get("charts") == 2


async def test_import_result_success_no_errors() -> None:
    """ImportResult.success is True when there are no errors."""
    result = ImportResult()
    assert result.success
    result.imported["charts"] = 3
    assert result.success


async def test_import_result_failure_with_errors() -> None:
    """ImportResult.success is False when errors are present."""
    result = ImportResult()
    result.errors.append("Something went wrong")
    assert not result.success


async def test_import_groups_by_type(manager: AsyncFullAssetManager) -> None:
    """Bundle entries are grouped by their top-level directory and counted
    per asset type in :class:`ImportResult`."""
    # Nested under the ``assets_export_<ts>/`` root that ``remove_root`` strips.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "assets_export_test/metadata.yaml", yaml.safe_dump({"version": "1.0.0"})
        )
        zf.writestr(
            "assets_export_test/charts/c1.yaml", yaml.safe_dump({"name": "c1"})
        )
        zf.writestr(
            "assets_export_test/dashboards/d1.yaml", yaml.safe_dump({"name": "d1"})
        )

    class _NoopAssetsCommand:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def execute(self) -> None:
            return None

    with patch(
        "superset.commands.importers.v1.ImportAssetsCommand", _NoopAssetsCommand
    ):
        result = await manager.import_assets(file_content=buf.getvalue())

    assert result.success, result.errors
    assert result.imported.get("charts") == 1
    assert result.imported.get("dashboards") == 1
