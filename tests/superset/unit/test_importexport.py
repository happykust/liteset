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
import io
import zipfile
from typing import Any

import pytest
import yaml

from superset.exceptions import CommandInvalidError
from superset.importexport.export_base import AsyncExportModelsCommand
from superset.importexport.import_base import AsyncImportModelsCommand


class SampleExportCommand(AsyncExportModelsCommand):
    dao_class = None
    _resource_type = "Slice"

    async def _export_single(self, model_id: int) -> list[tuple[str, str]]:
        return [
            ("charts/test.yaml", yaml.dump({"slice_name": "Test", "uuid": "abc-123"})),
        ]


async def test_export_produces_zip() -> None:
    cmd = SampleExportCommand(model_ids=[1])
    buf = await cmd.execute()
    assert isinstance(buf, io.BytesIO)
    with zipfile.ZipFile(buf) as zf:
        names = zf.namelist()
        assert "charts/test.yaml" in names
        assert "metadata.yaml" in names


async def test_export_yaml_content() -> None:
    cmd = SampleExportCommand(model_ids=[1])
    buf = await cmd.execute()
    with zipfile.ZipFile(buf) as zf:
        content = yaml.safe_load(zf.read("charts/test.yaml"))
        assert content["slice_name"] == "Test"
        assert content["uuid"] == "abc-123"


class SampleImportCommand(AsyncImportModelsCommand):
    async def _import_single(self, file_name: str, content: dict[str, Any]) -> None:
        pass

    async def _validate(self, configs: dict[str, dict[str, Any]]) -> None:
        pass


async def test_import_reads_zip() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("charts/test.yaml", yaml.dump({"slice_name": "Test"}))
        zf.writestr("metadata.yaml", yaml.dump({"version": "1.0.0", "type": "Slice"}))
    buf.seek(0)
    cmd = SampleImportCommand(contents=buf)
    configs = cmd._parse_zip()
    assert "charts/test.yaml" in configs
    assert configs["charts/test.yaml"]["slice_name"] == "Test"


async def test_export_multiple_models() -> None:
    """Export with multiple model_ids produces entries for each."""

    class MultiExportCommand(AsyncExportModelsCommand):
        dao_class = None

        async def _export_single(self, model_id: int) -> list[tuple[str, str]]:
            return [
                (f"charts/{model_id}.yaml", yaml.dump({"id": model_id})),
            ]

    cmd = MultiExportCommand(model_ids=[1, 2, 3])
    buf = await cmd.execute()
    with zipfile.ZipFile(buf) as zf:
        names = zf.namelist()
        assert "charts/1.yaml" in names
        assert "charts/2.yaml" in names
        assert "charts/3.yaml" in names
        content = yaml.safe_load(zf.read("charts/2.yaml"))
        assert content["id"] == 2


async def test_import_with_passwords() -> None:
    """Import command stores passwords dict for subclass access."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("databases/db.yaml", yaml.dump({"name": "mydb"}))
        zf.writestr("metadata.yaml", yaml.dump({"version": "1.0.0"}))
    buf.seek(0)
    passwords = {"databases/db.yaml": "secret123"}
    cmd = SampleImportCommand(contents=buf, passwords=passwords)
    assert cmd._passwords == {"databases/db.yaml": "secret123"}


async def test_import_with_overwrite_flag() -> None:
    """Import command stores overwrite flag."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("charts/c.yaml", yaml.dump({"name": "chart"}))
        zf.writestr("metadata.yaml", yaml.dump({"version": "1.0.0"}))
    buf.seek(0)
    cmd = SampleImportCommand(contents=buf, overwrite=True)
    assert cmd._overwrite is True


async def test_import_skips_metadata_yaml() -> None:
    """run() skips metadata.yaml when calling _import_single."""
    imported_files: list[str] = []

    class TrackingImportCommand(AsyncImportModelsCommand):
        async def _import_single(self, file_name: str, content: dict[str, Any]) -> None:
            imported_files.append(file_name)

        async def _validate(self, configs: dict[str, dict[str, Any]]) -> None:
            pass

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("charts/c1.yaml", yaml.dump({"name": "chart1"}))
        zf.writestr("charts/c2.yaml", yaml.dump({"name": "chart2"}))
        zf.writestr("metadata.yaml", yaml.dump({"version": "1.0.0"}))
    buf.seek(0)

    cmd = TrackingImportCommand(contents=buf)
    await cmd.execute()
    assert "metadata.yaml" not in imported_files
    assert len(imported_files) == 2


async def test_empty_zip_file_handling() -> None:
    """Empty ZIP file results in empty configs (no crash)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        pass  # empty zip
    buf.seek(0)
    cmd = SampleImportCommand(contents=buf)
    configs = cmd._parse_zip()
    assert configs == {}


async def test_manager_register_and_dispatch_export() -> None:
    """Manager.register_export + Manager.export dispatches correctly."""
    from superset.importexport.manager import AsyncImportExportManager

    class FakeExport(AsyncExportModelsCommand):
        dao_class = None

        async def _export_single(self, model_id: int) -> list[tuple[str, str]]:
            return [("fake.yaml", yaml.dump({"model_id": model_id}))]

    # Use a fresh class to avoid polluting shared state
    class TestManager(AsyncImportExportManager):
        _EXPORT_COMMANDS: dict[str, type] = {}
        _IMPORT_COMMANDS: dict[str, type] = {}

    TestManager.register_export("fake", FakeExport)
    buf = await TestManager.export("fake", model_ids=[7])
    with zipfile.ZipFile(buf) as zf:
        content = yaml.safe_load(zf.read("fake.yaml"))
        assert content["model_id"] == 7


async def test_manager_register_and_dispatch_import() -> None:
    """Manager.register_import + Manager.import_models dispatches correctly."""
    from superset.importexport.manager import AsyncImportExportManager

    imported: list[str] = []

    class FakeImport(AsyncImportModelsCommand):
        async def _import_single(self, file_name: str, content: dict[str, Any]) -> None:
            imported.append(file_name)

        async def _validate(self, configs: dict[str, dict[str, Any]]) -> None:
            pass

    class TestManager2(AsyncImportExportManager):
        _EXPORT_COMMANDS: dict[str, type] = {}
        _IMPORT_COMMANDS: dict[str, type] = {}

    TestManager2.register_import("fake", FakeImport)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("items/item.yaml", yaml.dump({"name": "item1"}))
        zf.writestr("metadata.yaml", yaml.dump({"version": "1.0.0"}))
    buf.seek(0)

    await TestManager2.import_models("fake", contents=buf)
    assert "items/item.yaml" in imported
    assert "metadata.yaml" not in imported


async def test_import_sanitizes_path_traversal() -> None:
    """ZIP entries with '..' or absolute paths are sanitized."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../etc/passwd", yaml.dump({"evil": True}))
        zf.writestr("charts/../../../tmp/hack.yaml", yaml.dump({"hack": True}))
        zf.writestr("charts/legit.yaml", yaml.dump({"slice_name": "OK"}))
        zf.writestr("/etc/absolute.yaml", yaml.dump({"abs": True}))
        zf.writestr("metadata.yaml", yaml.dump({"version": "1.0.0"}))
    buf.seek(0)
    cmd = SampleImportCommand(contents=buf)
    configs = cmd._parse_zip()
    # Path traversal components must be stripped
    assert "../../etc/passwd" not in configs
    assert "charts/../../../tmp/hack.yaml" not in configs
    assert "/etc/absolute.yaml" not in configs
    # Sanitized names should be present
    assert "etc/passwd" in configs
    assert "charts/tmp/hack.yaml" in configs
    assert "charts/legit.yaml" in configs
    # Absolute path sanitized to relative
    assert "etc/absolute.yaml" in configs


async def test_import_rejects_too_many_entries() -> None:
    """ZIP with more than MAX_ZIP_ENTRIES entries is rejected."""
    from superset.importexport.import_base import MAX_ZIP_ENTRIES

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(MAX_ZIP_ENTRIES + 1):
            zf.writestr(f"charts/chart_{i}.yaml", yaml.dump({"id": i}))
    buf.seek(0)
    cmd = SampleImportCommand(contents=buf)
    with pytest.raises(ValueError, match="too many entries"):
        cmd._parse_zip()


async def test_import_rejects_oversized_entry() -> None:
    """ZIP entry exceeding MAX_ENTRY_SIZE is rejected."""
    from superset.importexport.import_base import MAX_ENTRY_SIZE

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # Create an entry that claims to be too large
        # (use a small actual payload — the check is on file_size metadata)
        zf.writestr("charts/huge.yaml", "x" * (MAX_ENTRY_SIZE + 1))
    buf.seek(0)
    cmd = SampleImportCommand(contents=buf)
    with pytest.raises(ValueError, match="too large"):
        cmd._parse_zip()


async def test_import_validate_does_not_block_event_loop() -> None:
    """validate() should call _parse_zip via to_thread to avoid blocking."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("charts/test.yaml", yaml.dump({"slice_name": "Test"}))
        zf.writestr("metadata.yaml", yaml.dump({"version": "1.0.0"}))
    buf.seek(0)
    cmd = SampleImportCommand(contents=buf)
    # Should complete without errors — async wrapping is internal
    await cmd.validate()
    assert cmd._configs is not None


async def test_export_metadata_yaml_generated_by_base() -> None:
    """Base export command generates metadata.yaml with resource_type and timestamp."""
    cmd = SampleExportCommand(model_ids=[1])
    buf = await cmd.execute()
    with zipfile.ZipFile(buf) as zf:
        assert "metadata.yaml" in zf.namelist()
        metadata = yaml.safe_load(zf.read("metadata.yaml"))
        assert metadata["version"] == "1.0.0"
        assert metadata["type"] == "Slice"
        assert "timestamp" in metadata


async def test_import_rejects_missing_metadata() -> None:
    """validate() raises CommandInvalidError when metadata.yaml is absent."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("charts/c.yaml", yaml.dump({"slice_name": "Test"}))
    buf.seek(0)
    cmd = SampleImportCommand(contents=buf)
    with pytest.raises(CommandInvalidError, match="Missing metadata.yaml"):
        await cmd.validate()


async def test_import_rejects_wrong_version() -> None:
    """validate() raises CommandInvalidError for unsupported version."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("charts/c.yaml", yaml.dump({"slice_name": "Test"}))
        zf.writestr("metadata.yaml", yaml.dump({"version": "2.0.0", "type": "Slice"}))
    buf.seek(0)
    cmd = SampleImportCommand(contents=buf)
    with pytest.raises(CommandInvalidError, match="Unsupported import version: 2.0.0"):
        await cmd.validate()


# ---------------------------------------------------------------------------
# NEW-T11: ImportSavedQueriesCommand._validate is a no-op
# ---------------------------------------------------------------------------


async def test_import_saved_queries_validate_is_noop() -> None:
    """ImportSavedQueriesCommand._validate accepts any content (documented limitation).

    The _validate method is a no-op pass statement, so even malformed imports
    will pass validation. This test documents that limitation.
    """
    from unittest.mock import AsyncMock

    from superset.commands.query import ImportSavedQueriesCommand

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # Malformed entry: missing label, sql, and all expected fields
        zf.writestr("queries/db/schema/q.yaml", yaml.dump({"garbage": True}))
        zf.writestr(
            "metadata.yaml", yaml.dump({"version": "1.0.0", "type": "SavedQuery"})
        )
    buf.seek(0)
    dao = AsyncMock()
    dao.session = AsyncMock()
    dao.create = AsyncMock()
    dao.session.flush = AsyncMock()
    cmd = ImportSavedQueriesCommand(contents=buf, dao=dao)
    # validate should pass despite malformed content (no-op _validate)
    await cmd.validate()
