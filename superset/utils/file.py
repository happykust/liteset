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

"""Filename helpers — drop-in replacement for the upstream
secure-filename helper (``superset_old/utils/file.py``).

Liteset has no legacy WSGI runtime dependency, so we ship a
behaviour-compatible :func:`secure_filename`.  The implementation
mirrors the upstream secure-filename helper: NFKD-normalise the
input, drop non-ASCII, collapse path separators, strip everything that
isn't alphanumeric / underscore / dash / dot, and prefix Windows
reserved names so they round-trip safely.
"""

from __future__ import annotations

import os
import re
import unicodedata

# Match anything that's not alphanumeric, underscore, dot, or dash.
# Same character class the upstream helper uses internally.
_FILENAME_ASCII_STRIP_RE = re.compile(r"[^A-Za-z0-9_.-]")

# Windows reserved device names — the upstream helper prefixes these with an
# underscore so the resulting string can't accidentally name a device
# file when written to a Windows filesystem.
_WINDOWS_DEVICE_FILES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


def secure_filename(filename: str) -> str:
    """Return a sanitized filename safe to use on any filesystem.

    Drop-in replacement for the upstream secure-filename helper:

    * Unicode is normalised via NFKD and stripped to ASCII.
    * OS path separators (``/``, and ``\\`` on Windows) are replaced
      with spaces, then collapsed into single underscores.
    * Anything outside ``[A-Za-z0-9_.-]`` is removed.
    * Leading dots and underscores are stripped.
    * Windows reserved device names (``CON``, ``PRN``, ``COM1`` …) get
      an underscore prefix so they can be safely written as files.
    """
    filename = unicodedata.normalize("NFKD", filename)
    filename = filename.encode("ascii", "ignore").decode("ascii")

    for sep in (os.sep, os.path.altsep):
        if sep:
            filename = filename.replace(sep, " ")
    filename = str(_FILENAME_ASCII_STRIP_RE.sub("", "_".join(filename.split()))).strip(
        "._"
    )

    # On Windows, prefix reserved device names so they can be used
    # as file names without accidentally addressing the device.
    if (
        os.name == "nt"
        and filename
        and filename.split(".")[0].upper() in _WINDOWS_DEVICE_FILES
    ):
        filename = f"_{filename}"

    return filename


def get_filename(model_name: str, model_id: int, skip_id: bool = False) -> str:
    """Build a filesystem-safe filename for a model export.

    Verbatim port of ``superset_old.utils.file.get_filename``: slug the
    model name via :func:`secure_filename`, append the id (unless
    ``skip_id`` is set), and fall back to the bare id if the name
    sluggified to an empty string.
    """
    slug = secure_filename(model_name)
    filename = slug if skip_id else f"{slug}_{model_id}"
    return filename if slug else str(model_id)
