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
"""Async port of ``superset_old/commands/database/uploaders/columnar_reader.py``.

Mirrors the original Parquet/columnar reader 1:1, including the ZIP
multi-file support.  ``file`` is normalised via :func:`_to_stream` so the
reader accepts Litestar ``UploadFile`` / Werkzeug ``FileStorage`` /
``IO[bytes]`` / raw bytes inputs.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from io import BytesIO
from pathlib import Path
from typing import Any, IO, Optional
from zipfile import BadZipfile, is_zipfile, ZipFile

import pandas as pd
import pyarrow.parquet as pq
from pyarrow.lib import ArrowException

from superset.commands.database.exceptions import DatabaseUploadFailed
from superset.commands.database.uploaders.base import (
    BaseDataReader,
    FileMetadata,
    ReaderOptions,
)
from superset.commands.database.uploaders.csv_reader import _to_stream
from superset.i18n import gettext as _

logger = logging.getLogger(__name__)


class ColumnarReaderOptions(ReaderOptions, total=False):
    columns_read: list[str]


class ColumnarReader(BaseDataReader):
    def __init__(
        self,
        options: Optional[ColumnarReaderOptions] = None,
    ) -> None:
        options = options or {}
        super().__init__(
            options=dict(options),
        )

    def _read_buffer_to_dataframe(self, buffer: IO[bytes]) -> pd.DataFrame:
        kwargs: dict[str, Any] = {
            "path": buffer,
        }
        if self._options.get("columns_read"):
            kwargs["columns"] = self._options.get("columns_read")
        try:
            return pd.read_parquet(**kwargs)
        except (
            pd.errors.ParserError,
            pd.errors.EmptyDataError,
            UnicodeDecodeError,
            ValueError,
        ) as ex:
            raise DatabaseUploadFailed(
                message=_("Parsing error: %(error)s", error=str(ex))
            ) from ex
        except Exception as ex:
            raise DatabaseUploadFailed(_("Error reading Columnar file")) from ex

    @staticmethod
    def _yield_files(file: Any) -> Generator[IO[bytes], None, None]:
        """Yield each parquet payload from ``file`` (transparently unwraps ZIPs)."""
        # The original used ``file.filename`` (FileStorage attribute) — fall
        # back to ``file.filename``, ``file.name``, or the original ``file``
        # itself depending on the input type.
        filename = getattr(file, "filename", None) or getattr(file, "name", None)
        if not filename:
            raise DatabaseUploadFailed(_("Unexpected no file extension found"))
        file_suffix = Path(filename).suffix
        if not file_suffix:
            raise DatabaseUploadFailed(_("Unexpected no file extension found"))
        file_suffix = file_suffix[1:]  # strip the leading dot

        if file_suffix == "zip":
            stream = _to_stream(file)
            if not is_zipfile(stream):
                raise DatabaseUploadFailed(_("Not a valid ZIP file"))
            try:
                # ``is_zipfile`` consumes some of the stream — rewind.
                stream.seek(0)
                with ZipFile(stream) as zip_file:
                    file_suffixes = {Path(name).suffix for name in zip_file.namelist()}
                    if len(file_suffixes) > 1:
                        raise DatabaseUploadFailed(
                            _("ZIP file contains multiple file types")
                        )
                    for filename_in_zip in zip_file.namelist():
                        with zip_file.open(filename_in_zip) as file_in_zip:
                            yield BytesIO(file_in_zip.read())
            except BadZipfile as ex:
                raise DatabaseUploadFailed(_("Not a valid ZIP file")) from ex
        else:
            yield _to_stream(file)

    def file_to_dataframe(self, file: Any) -> pd.DataFrame:
        return pd.concat(
            self._read_buffer_to_dataframe(buffer) for buffer in self._yield_files(file)
        )

    def file_metadata(self, file: Any) -> FileMetadata:
        column_names: set[str] = set()
        try:
            for file_item in self._yield_files(file):
                parquet_file = pq.ParquetFile(file_item)
                column_names.update(parquet_file.metadata.schema.names)  # pylint: disable=no-member
        except ArrowException as ex:
            raise DatabaseUploadFailed(
                message=_("Parsing error: %(error)s", error=str(ex))
            ) from ex
        return {
            "items": [
                {
                    "column_names": list(column_names),
                    "sheet_name": None,
                }
            ]
        }
