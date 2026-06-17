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
"""Unit tests for superset.commands.importers.v1.utils.

Covers two audit findings:

Finding 1 — zip-bomb zero-guard:
  The original check_is_safe_zip performs an unconditional division
  ``compress_ratio = uncompress_size / compress_size`` which raises
  ZeroDivisionError for empty/directory-only ZIPs.  That is a bug, not
  intended behaviour.  The liteset implementation correctly guards against it;
  for a pathologically empty ZIP the bundle is effectively treated as having no
  valid entries (get_contents_from_bundle returns {}).  Verdict: false_positive.

Finding 2 — timestamp datetime acceptance:
  MetadataSchema uses ``fields.DateTime()`` which, contrary to the audit claim,
  does NOT accept Python datetime objects.  When YAML safe_load produces a
  datetime from an unquoted timestamp literal the original also raises a
  ValidationError('Not a valid datetime.').  Liteset behaviour is identical:
  the same validation error is raised.  Verdict: false_positive.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime

import pytest
import yaml

from superset.commands.importers.exceptions import IncorrectVersionError
from superset.commands.importers.v1.utils import (
    get_contents_from_bundle,
    load_metadata,
    METADATA_FILE_NAME,
)
from superset.exceptions import CommandInvalidError

# ---------------------------------------------------------------------------
# Finding 2 — load_metadata timestamp validation
# ---------------------------------------------------------------------------


def test_load_metadata_accepts_string_timestamp() -> None:
    """metadata.yaml with an ISO-formatted STRING timestamp is accepted.

    Machine-generated bundles always serialise via datetime.isoformat() which
    yaml.dump quotes; safe_load then returns a str.  This is the hot path.
    """
    contents = {
        METADATA_FILE_NAME: yaml.dump(
            {
                "version": "1.0.0",
                "type": "Slice",
                "timestamp": "2024-01-15T00:00:00+00:00",
            }
        )
    }
    metadata = load_metadata(contents)
    assert metadata["version"] == "1.0.0"
    assert metadata["type"] == "Slice"
    assert isinstance(metadata["timestamp"], str)


def test_load_metadata_accepts_missing_timestamp() -> None:
    """timestamp is optional in metadata.yaml — absence is accepted."""
    contents = {METADATA_FILE_NAME: yaml.dump({"version": "1.0.0", "type": "Slice"})}
    metadata = load_metadata(contents)
    assert metadata["version"] == "1.0.0"
    assert "timestamp" not in metadata


def test_load_metadata_rejects_yaml_datetime_object() -> None:
    """Unquoted YAML datetime literals are rejected exactly as in the original.

    ``yaml.safe_load`` parses ``timestamp: 2024-01-15T00:00:00`` (no quotes)
    into a Python datetime object.

    ``MetadataSchema().load(metadata)`` → Marshmallow DateTime._deserialize
    calls ``from_iso_datetime(datetime_obj)`` → regex.match raises TypeError
    → caught → ValidationError({'timestamp': ['Not a valid datetime.']}).
    Since 'version' is NOT in ex.messages the ValidationError propagates.

    Liteset behaviour: a non-string timestamp triggers
    ``errors["timestamp"] = ["Not a valid datetime."]`` → CommandInvalidError.
    Both produce a 422 with the same message.
    """
    # Build a YAML string that safe_load will parse as a datetime object.
    raw_yaml = "version: '1.0.0'\ntimestamp: 2024-01-15T00:00:00\n"
    parsed = yaml.safe_load(raw_yaml)
    # Confirm yaml parsed the timestamp into a Python datetime.
    assert isinstance(parsed["timestamp"], datetime), (
        "yaml.safe_load should produce a datetime for unquoted ISO literals"
    )

    contents = {METADATA_FILE_NAME: raw_yaml}
    with pytest.raises(CommandInvalidError) as exc_info:
        load_metadata(contents)

    # CommandInvalidError stores the structured dict in extra["errors"].
    errors = exc_info.value.extra["errors"]
    assert METADATA_FILE_NAME in errors
    assert "timestamp" in errors[METADATA_FILE_NAME]
    assert "Not a valid datetime." in errors[METADATA_FILE_NAME]["timestamp"]


def test_load_metadata_rejects_integer_timestamp() -> None:
    """A non-string, non-absent timestamp value (e.g. integer) is rejected."""
    contents = {METADATA_FILE_NAME: yaml.dump({"version": "1.0.0", "timestamp": 12345})}
    with pytest.raises(CommandInvalidError) as exc_info:
        load_metadata(contents)

    errors = exc_info.value.extra["errors"]
    assert METADATA_FILE_NAME in errors
    assert "Not a valid datetime." in errors[METADATA_FILE_NAME]["timestamp"]


def test_load_metadata_raises_incorrect_version_error_for_missing_metadata_file() -> (
    None
):
    """Missing metadata.yaml raises IncorrectVersionError (original parity)."""
    with pytest.raises(IncorrectVersionError):
        load_metadata({})


def test_load_metadata_raises_incorrect_version_error_for_wrong_version() -> None:
    """Wrong version in metadata.yaml raises IncorrectVersionError."""
    contents = {METADATA_FILE_NAME: yaml.dump({"version": "2.0.0"})}
    with pytest.raises(IncorrectVersionError):
        load_metadata(contents)


# ---------------------------------------------------------------------------
# Finding 1 — zip-bomb zero-guard (empty ZIP)
# ---------------------------------------------------------------------------


def test_get_contents_from_bundle_returns_empty_for_empty_zip() -> None:
    """An empty ZIP (no entries) returns an empty dict without crashing.

    check_is_safe_zip raised ZeroDivisionError when compress_size == 0
    (unintentional bug).

    Liteset behaviour: compress_size guard skips the ratio check; the ZIP has
    no YAML entries so get_contents_from_bundle returns {}.  This is strictly
    better and the caller can raise NoValidFilesFoundError or equivalent.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        pass  # deliberately empty
    buf.seek(0)
    with zipfile.ZipFile(buf) as zf:
        result = get_contents_from_bundle(zf)
    assert result == {}


def test_get_contents_from_bundle_returns_empty_for_directory_only_zip() -> None:
    """ZIP containing only directory entries (compress_size == 0) does not crash."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # ZipInfo with is_dir will have file_size=0, compress_size=0
        zf.mkdir("charts/")
    buf.seek(0)
    with zipfile.ZipFile(buf) as zf:
        result = get_contents_from_bundle(zf)
    # Directories are not valid YAML configs so the result is empty.
    assert result == {}


def test_get_contents_from_bundle_reads_yaml_entries() -> None:
    """ZIP with YAML entries returns their decoded text content."""
    payload = yaml.dump({"slice_name": "my chart", "version": "1.0.0"})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bundle/charts/c1.yaml", payload)
        zf.writestr("bundle/metadata.yaml", yaml.dump({"version": "1.0.0"}))
    buf.seek(0)
    with zipfile.ZipFile(buf) as zf:
        result = get_contents_from_bundle(zf)
    # remove_root strips the leading "bundle/" segment.
    assert "charts/c1.yaml" in result
    assert "metadata.yaml" in result
    assert "slice_name" in result["charts/c1.yaml"]


def test_get_contents_from_bundle_rejects_too_many_entries() -> None:
    """ZIP with > 1000 entries is rejected with IncorrectFormatError."""
    from superset.commands.importers.exceptions import IncorrectFormatError

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(1001):
            zf.writestr(f"bundle/charts/c{i}.yaml", f"id: {i}")
    buf.seek(0)
    with zipfile.ZipFile(buf) as zf:
        with pytest.raises(IncorrectFormatError):
            get_contents_from_bundle(zf)


def test_get_contents_from_bundle_rejects_path_traversal() -> None:
    """ZIP entry with ``..`` in path raises IncorrectFormatError."""
    from superset.commands.importers.exceptions import IncorrectFormatError

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("bundle/../etc/passwd", "evil")
    buf.seek(0)
    with zipfile.ZipFile(buf) as zf:
        with pytest.raises(IncorrectFormatError):
            get_contents_from_bundle(zf)
