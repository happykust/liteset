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
# mypy: ignore-errors
"""Async port of ``superset_old/commands/database/uploaders/base.py``.

Hosts:

* :class:`UploadCommand` — async port of the original ``UploadCommand``
  (Liteset wrapped the entire file-to-table flow into a single command
  early in the migration, before the per-format readers were ported).
* :class:`BaseDataReader`, :class:`ReaderOptions`, :class:`FileMetadata`,
  :class:`UploadFileType` — verbatim 1:1 ports of the legacy module so
  the per-format readers (``csv_reader.py``, ``excel_reader.py``,
  ``columnar_reader.py``) can extend them.
"""

from __future__ import annotations

import io
import logging
from abc import abstractmethod
from typing import Any, Optional, TYPE_CHECKING, TypedDict

import pandas as pd

from superset.commands.base import AsyncBaseCommand
from superset.commands.database.exceptions import DatabaseUploadFailed
from superset.exceptions import CommandInvalidError, ObjectNotFoundError
from superset.i18n import gettext as _
from superset.utils.backports import StrEnum

if TYPE_CHECKING:
    from superset.db.daos.database import AsyncDatabaseDAO

logger = logging.getLogger(__name__)

READ_CHUNK_SIZE = 1000


class UploadFileType(StrEnum):
    CSV = "csv"
    EXCEL = "excel"
    COLUMNAR = "columnar"


class ReaderOptions(TypedDict, total=False):
    already_exists: str
    index_label: str
    dataframe_index: bool


class FileMetadataItem(TypedDict):
    sheet_name: Optional[str]
    column_names: list[str]


class FileMetadata(TypedDict, total=False):
    items: list[FileMetadataItem]


class BaseDataReader:
    """
    Base class for reading data from a file and uploading it to a database.

    These child objects are used by the UploadCommand as a dependency
    injection to read data from multiple file types (e.g. CSV, Excel, etc.)

    Async port of
    ``superset_old.commands.database.uploaders.base.BaseDataReader``.
    """

    def __init__(self, options: Optional[dict[str, Any]] = None) -> None:
        self._options = options or {}

    @abstractmethod
    def file_to_dataframe(self, file: Any) -> pd.DataFrame: ...

    @abstractmethod
    def file_metadata(self, file: Any) -> FileMetadata: ...

    def read(
        self,
        file: Any,
        database: Any,
        table_name: str,
        schema_name: Optional[str],
    ) -> None:
        self._dataframe_to_database(
            self.file_to_dataframe(file), database, table_name, schema_name
        )

    def _dataframe_to_database(
        self,
        df: pd.DataFrame,
        database: Any,
        table_name: str,
        schema_name: Optional[str],
    ) -> None:
        """Upload DataFrame to database via the engine spec's ``df_to_sql``."""
        from superset.sql.parse import Table

        try:
            data_table = Table(table=table_name, schema=schema_name)
            to_sql_kwargs = {
                "chunksize": READ_CHUNK_SIZE,
                "if_exists": self._options.get("already_exists", "fail"),
                "index": self._options.get("dataframe_index", False),
            }
            if self._options.get("index_label") and self._options.get(
                "dataframe_index"
            ):
                to_sql_kwargs["index_label"] = self._options.get("index_label")
            database.db_engine_spec.df_to_sql(
                database,
                data_table,
                df,
                to_sql_kwargs=to_sql_kwargs,
            )
        except ValueError as ex:
            raise DatabaseUploadFailed(
                message=_(
                    "Table already exists. You can change your "
                    "'if table already exists' strategy to append or "
                    "replace or provide a different Table Name to use."
                )
            ) from ex
        except Exception as ex:
            message = ex.message if hasattr(ex, "message") and ex.message else str(ex)
            raise DatabaseUploadFailed(
                message=_(
                    "Failed loading data into the database: %(error)s",
                    error=message,
                )
            ) from ex


class UploadCommand(AsyncBaseCommand[dict[str, Any]]):
    """Upload a file to a database as a new table.

    Liteset extension that wraps the file-to-table flow into a single
    Command for simple call sites.  The per-format readers
    (``csv_reader``/``excel_reader``/``columnar_reader``) provide the
    parsing layer; this Command only orchestrates the upload + SqlaTable
    bookkeeping.
    """

    def __init__(
        self,
        dao: AsyncDatabaseDAO,
        database_id: int,
        data: dict[str, Any],
        file_contents: bytes,
    ) -> None:
        self._dao = dao
        self._database_id = database_id
        self._data = data
        self._file_contents = file_contents
        self._database: Any | None = None

    async def validate(self) -> None:
        if not self._data.get("table_name"):
            raise CommandInvalidError("table_name is required")
        self._database = await self._dao.find_by_id(self._database_id)
        if not self._database:
            raise ObjectNotFoundError("Database", self._database_id)

        # Check if file upload is allowed for this database/schema
        if not getattr(self._database, "allow_file_upload", False):
            raise CommandInvalidError("File upload is not enabled for this database")

    async def run(self) -> dict[str, Any]:
        from superset.sql.parse import Table

        table_name = self._data["table_name"]
        schema_name = self._data.get("schema")
        file_type = self._data.get("file_type", "csv")

        # Read file into DataFrame
        df = self._read_file(file_type)

        # Upload DataFrame to database
        data_table = Table(table=table_name, schema=schema_name)
        to_sql_kwargs = {
            "chunksize": 1000,
            "if_exists": self._data.get("if_exists", "fail"),
            "index": self._data.get("dataframe_index", False),
        }
        if self._data.get("index_label") and self._data.get("dataframe_index"):
            to_sql_kwargs["index_label"] = self._data["index_label"]

        self._database.db_engine_spec.df_to_sql(
            self._database,
            data_table,
            df,
            to_sql_kwargs=to_sql_kwargs,
        )

        # Create or update SqlaTable entry
        from sqlalchemy import select

        from superset.models.connectors import SqlaTable

        stmt = select(SqlaTable).where(
            SqlaTable.table_name == table_name,
            SqlaTable.schema == schema_name,
            SqlaTable.database_id == self._database_id,
        )
        result = await self._dao.session.execute(stmt)
        sqla_table = result.scalars().one_or_none()

        if not sqla_table:
            sqla_table = SqlaTable(
                table_name=table_name,
                database_id=self._database_id,
                schema=schema_name,
            )
            self._dao.session.add(sqla_table)

        await self._dao.session.flush()

        # Mirror the original API contract — ``self.response(201, message="OK")``
        # (superset_old/databases/api.py:1787). The original endpoint returns
        # only ``{"message": "OK"}``; ``table_id`` is not part of the response.
        return {"message": "OK"}

    def _read_file(self, file_type: str) -> pd.DataFrame:
        """Read file contents into a pandas DataFrame."""
        file_obj = io.BytesIO(self._file_contents)

        if file_type == "csv":
            return pd.read_csv(file_obj)
        if file_type == "excel":
            return pd.read_excel(file_obj)
        if file_type == "columnar":
            return pd.read_parquet(file_obj)
        raise CommandInvalidError(f"Unsupported file type: {file_type}")
