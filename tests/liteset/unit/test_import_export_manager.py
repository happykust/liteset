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
from unittest.mock import AsyncMock

import pytest
import yaml

from liteset.importexport.manager import AsyncFullAssetManager, ImportResult


@pytest.fixture
def mock_session() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def manager(mock_session: AsyncMock) -> AsyncFullAssetManager:
    return AsyncFullAssetManager(mock_session)


async def test_export_creates_valid_zip(manager: AsyncFullAssetManager) -> None:
    """export_assets returns bytes that form a valid ZIP with metadata."""
    data = await manager.export_assets()
    assert isinstance(data, bytes)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        assert "metadata.yaml" in names
        metadata = yaml.safe_load(zf.read("metadata.yaml"))
        assert metadata["version"] == "1.0.0"
        assert metadata["type"] == "assets"
        assert "timestamp" in metadata


async def test_export_with_asset_types(manager: AsyncFullAssetManager) -> None:
    """export_assets respects asset_types filter."""
    data = await manager.export_assets(asset_types=["charts"])
    assert isinstance(data, bytes)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        # Only metadata should be present since _export_type is a placeholder
        assert "metadata.yaml" in zf.namelist()


async def test_import_handles_bad_zip(manager: AsyncFullAssetManager) -> None:
    """import_assets returns error for invalid ZIP content."""
    result = await manager.import_assets(file_content=b"not a zip file")
    assert not result.success
    assert any("Invalid ZIP" in e for e in result.errors)


async def test_import_valid_zip(manager: AsyncFullAssetManager) -> None:
    """import_assets processes a valid ZIP without errors."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "metadata.yaml",
            yaml.dump({"version": "1.0.0", "type": "assets"}),
        )
        zf.writestr("charts/chart1.yaml", yaml.dump({"name": "test_chart"}))
    result = await manager.import_assets(file_content=buf.getvalue())
    assert result.success
    # Placeholder _import_type returns 0
    assert result.imported.get("charts") == 0


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
    """import_assets groups entries by top-level directory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("metadata.yaml", yaml.dump({"version": "1.0.0"}))
        zf.writestr("charts/c1.yaml", yaml.dump({"name": "c1"}))
        zf.writestr("charts/c2.yaml", yaml.dump({"name": "c2"}))
        zf.writestr("dashboards/d1.yaml", yaml.dump({"name": "d1"}))
    result = await manager.import_assets(file_content=buf.getvalue())
    assert result.success
    assert "charts" in result.imported
    assert "dashboards" in result.imported


async def test_export_empty_placeholder(manager: AsyncFullAssetManager) -> None:
    """_export_type placeholder returns empty list, so ZIP has only metadata."""
    data = await manager.export_assets()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        # Only metadata.yaml should be present
        assert zf.namelist() == ["metadata.yaml"]
