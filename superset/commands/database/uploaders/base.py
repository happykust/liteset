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
"""Base classes and command for uploading files to a database as a new table.

Hosts:

* :class:`UploadCommand` — orchestrates the file-to-table upload flow.
* :class:`BaseDataReader`, :class:`ReaderOptions`, :class:`FileMetadata`,
  :class:`UploadFileType` — extended by the per-format readers
  (``csv_reader.py``, ``excel_reader.py``, ``columnar_reader.py``).
"""

from __future__ import annotations

import io
import logging
from abc import abstractmethod
from typing import Any, Optional, TYPE_CHECKING, TypedDict

import pandas as pd

from superset.commands.base import AsyncBaseCommand
from superset.commands.database.exceptions import (
    DatabaseSchemaUploadNotAllowed,
    DatabaseUploadFailed,
    DatabaseUploadNotSupported,
)
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
    """Base reader: subclasses implement per-format parsing for UploadCommand."""

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
    """Upload a file to a database as a new table."""

    def __init__(
        self,
        dao: AsyncDatabaseDAO,
        database_id: int,
        data: dict[str, Any],
        file_contents: bytes,
        filename: str = "",
        security_manager: Any | None = None,
        current_user: Any | None = None,
    ) -> None:
        self._dao = dao
        self._database_id = database_id
        self._data = data
        self._file_contents = file_contents
        self._filename = filename
        self._security_manager = security_manager
        self._current_user = current_user
        self._database: Any | None = None

    async def validate(self) -> None:
        if not self._data.get("table_name"):
            raise CommandInvalidError("table_name is required")
        self._database = await self._dao.find_by_id(self._database_id)
        if not self._database:
            raise ObjectNotFoundError("Database", self._database_id)

        schema = self._data.get("schema")
        allowed = bool(getattr(self._database, "allow_file_upload", False))
        if allowed:
            # ``get_schema_access_for_file_upload`` handles legacy string-encoded
            # allowlists via ``literal_eval`` AND the configurable
            # ``ALLOWED_USER_CSV_SCHEMA_FUNC`` hook.
            try:
                schemas_allowed = self._database.get_schema_access_for_file_upload()
            except Exception:  # noqa: BLE001
                schemas_allowed = set()
            if schemas_allowed:
                allowed = schema in schemas_allowed
            # no schema restriction → check DB-level access
            elif self._security_manager is not None:
                allowed = await self._security_manager.can_access_database(
                    self._database, user=self._current_user
                )
        if not allowed:
            raise DatabaseSchemaUploadNotAllowed()

        if not getattr(self._database.db_engine_spec, "supports_file_upload", True):
            raise DatabaseUploadNotSupported()

    async def run(self) -> dict[str, Any]:
        import asyncio

        table_name = self._data["table_name"]
        schema_name = self._data.get("schema")

        reader = self._build_reader()
        bio = io.BytesIO(self._file_contents)
        # The columnar reader sniffs ``filename`` for zip/extension detection.
        bio.name = self._filename or table_name  # type: ignore[attr-defined]
        await asyncio.to_thread(
            reader.read, bio, self._database, table_name, schema_name
        )

        from sqlalchemy import select

        from superset.db.daos.dataset import AsyncDatasetDAO
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
            # Pre-init to avoid sync lazy-load on the transient object.
            sqla_table.owners = [self._current_user] if self._current_user else []
            self._dao.session.add(sqla_table)

        await self._dao.session.flush()

        from superset.commands.database.exceptions import (
            DatabaseUploadSaveMetadataFailed,
        )

        try:
            await AsyncDatasetDAO(self._dao.session).fetch_metadata(sqla_table)
        except Exception as ex:
            logger.warning("fetch_metadata failed for uploaded table", exc_info=True)
            raise DatabaseUploadSaveMetadataFailed() from ex

        return {"message": "OK"}

    def _build_reader(self) -> "BaseDataReader":
        """Select the per-format reader."""
        from superset.commands.database.uploaders.columnar_reader import ColumnarReader
        from superset.commands.database.uploaders.csv_reader import CSVReader
        from superset.commands.database.uploaders.excel_reader import ExcelReader

        file_type = self._data.get("file_type") or self._data.get("type") or "csv"
        options = dict(self._data)
        if file_type == "csv":
            return CSVReader(options)  # type: ignore[arg-type]
        if file_type == "excel":
            return ExcelReader(options)  # type: ignore[arg-type]
        if file_type == "columnar":
            return ColumnarReader(options)  # type: ignore[arg-type]
        raise CommandInvalidError(f"Unsupported file type: {file_type}")
