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

"""Filename sanitization helpers.

:func:`secure_filename` NFKD-normalises the input, drops non-ASCII
characters, collapses path separators, strips everything that is not
alphanumeric / underscore / dash / dot, and prefixes Windows reserved
names so they round-trip safely.
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

    if (
        os.name == "nt"
        and filename
        and filename.split(".")[0].upper() in _WINDOWS_DEVICE_FILES
    ):
        filename = f"_{filename}"

    return filename


def get_filename(model_name: str, model_id: int, skip_id: bool = False) -> str:
    """Build a filesystem-safe filename for a model export.

    Slugs the model name via :func:`secure_filename`, appends the id
    (unless ``skip_id`` is set), and falls back to the bare id if the
    name sluggified to an empty string.
    """
    slug = secure_filename(model_name)
    filename = slug if skip_id else f"{slug}_{model_id}"
    return filename if slug else str(model_id)
