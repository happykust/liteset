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
"""Regression: an oversized ZIP entry must be rejected from its header
metadata BEFORE it is decompressed, not after.

``AsyncImportModelsCommand._parse_zip`` used to call ``zf.read(name)`` and
only THEN check ``len(raw) > MAX_ENTRY_SIZE`` — so a small, highly
compressible upload could still force a multi-gigabyte allocation. The
archive-total compress-ratio guard doesn't catch this either: padding the
bundle with incompressible filler entries keeps the *total* ratio legal
while one member's declared size stays unbounded.

This test uses near-incompressible (random) filler so the per-entry
compress ratio for the oversized member itself stays close to 1:1 (legal),
proving the header-based ``ZipInfo.file_size`` check — not the ratio
check — is what catches it.
"""

from __future__ import annotations

import io
import os
import zipfile
from typing import Any

import pytest


async def test_oversized_entry_rejected_without_decompressing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from superset.importexport.import_base import AsyncImportModelsCommand

    # Shrink the threshold so the test doesn't need a real 50MB payload.
    monkeypatch.setattr(
        "superset.importexport.import_base.MAX_ENTRY_SIZE", 1024, raising=True
    )

    class FakeImportCommand(AsyncImportModelsCommand):
        async def _import_single(self, file_name: str, content: dict[str, Any]) -> None:
            pass

        async def _validate(self, configs: dict[str, dict[str, Any]]) -> None:
            pass

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("metadata.yaml", "version: 1.0.0\ntype: Slice\n")
        # Random (near-incompressible) filler just over the patched
        # threshold: its OWN compress ratio stays ~1:1, and the archive
        # total stays well under the default 200x ratio cap, so only the
        # header-based per-entry size check can catch this.
        oversized = os.urandom(2048)
        zf.writestr("charts/bomb.yaml", oversized)
    buf.seek(0)

    cmd = FakeImportCommand(contents=buf)

    # Instrument ``ZipFile.read`` so a member being decompressed at all is
    # directly observable. Both the header check (before the entry loop)
    # and a leftover post-``zf.read`` size check (inside the loop) raise
    # the identical "too large" message, so ``pytest.raises(..., match=
    # "too large")`` alone cannot tell which one fired -- only whether the
    # oversized member was ever read proves the header check caught it
    # first, without decompressing it.
    read_calls: list[str] = []
    original_read = zipfile.ZipFile.read

    def _tracking_read(self: zipfile.ZipFile, name: Any, *a: Any, **kw: Any) -> bytes:
        read_calls.append(name if isinstance(name, str) else name.filename)
        return original_read(self, name, *a, **kw)

    monkeypatch.setattr(zipfile.ZipFile, "read", _tracking_read)

    with pytest.raises(ValueError, match="too large"):
        cmd._parse_zip()

    assert read_calls == [], (
        f"the oversized entry should be rejected from ZipInfo header "
        f"metadata before any entry is decompressed, but read() was "
        f"called for: {read_calls}"
    )


async def test_small_entries_are_not_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity check: the new guard doesn't reject legitimate small bundles."""
    from superset.importexport.import_base import AsyncImportModelsCommand

    monkeypatch.setattr(
        "superset.importexport.import_base.MAX_ENTRY_SIZE", 1024, raising=True
    )

    class FakeImportCommand(AsyncImportModelsCommand):
        async def _import_single(self, file_name: str, content: dict[str, Any]) -> None:
            pass

        async def _validate(self, configs: dict[str, dict[str, Any]]) -> None:
            pass

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("metadata.yaml", "version: 1.0.0\ntype: Slice\n")
        zf.writestr("charts/small.yaml", "slice_name: Small\n")
    buf.seek(0)

    cmd = FakeImportCommand(contents=buf)
    configs = cmd._parse_zip()
    # ``remove_root`` strips the first path segment (matches ``_parse_zip``'s
    # documented behaviour / ``test_import_reads_zip`` in test_importexport.py).
    assert "small.yaml" in configs
