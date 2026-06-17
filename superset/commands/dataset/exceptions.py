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
"""Dataset-specific exception classes."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from superset.exceptions import (
    CommandException,
    CommandInvalidError,
    ImportFailedError,
    ObjectNotFoundError,
)
from superset.i18n import gettext as _

if TYPE_CHECKING:
    from superset.sql.parse import Table


def get_dataset_exist_error_msg(table: Table) -> str:
    return _("Dataset %(table)s already exists", table=table)


class DatasetValidationError(CommandInvalidError):
    """Lightweight per-field dataset validation error.

    Each instance carries a ``field_name`` and a list of human-readable
    messages so an accumulating :class:`DatasetInvalidError` can merge them
    into the ``{field: [messages]}`` 422 body via ``normalized_messages()``.
    """

    status_code = 422
    # "_schema" is the Marshmallow key for field-less ValidationErrors; kept for
    # compatibility with any consumer that parses the per-field 422 body shape.
    field_name: str = "_schema"

    def __init__(
        self,
        messages: str | list[str] | None = None,
        field_name: str | None = None,
    ) -> None:
        if messages is None:
            messages = [str(self.message)]
        elif isinstance(messages, str):
            messages = [messages]
        self.messages: list[str] = [str(message) for message in messages]
        if field_name is not None:
            self.field_name = field_name
        super().__init__(message="; ".join(self.messages))

    def normalized_messages(self) -> dict[str, list[str]]:
        return {self.field_name: list(self.messages)}


class MultiCatalogDisabledValidationError(DatasetValidationError):
    """Raised when the connection does not support multiple catalogs."""

    field_name = "catalog"

    def __init__(self) -> None:
        super().__init__(
            [_("Only the default catalog is supported for this connection")]
        )


class DatabaseNotFoundValidationError(DatasetValidationError):
    """Raised when the specified database does not exist."""

    field_name = "database"

    def __init__(self) -> None:
        super().__init__([_("Database does not exist")])


class DatasetExistsValidationError(DatasetValidationError):
    """Raised when a dataset with the given table name already exists."""

    field_name = "table"

    def __init__(self, table: Table) -> None:
        super().__init__([get_dataset_exist_error_msg(table)])


class DatasetColumnNotFoundValidationError(DatasetValidationError):
    """Raised when one or more submitted column IDs do not exist on the dataset."""

    field_name = "columns"

    def __init__(self) -> None:
        super().__init__([_("One or more columns do not exist")])


class DatasetColumnsDuplicateValidationError(DatasetValidationError):
    """Raised when submitted columns contain duplicate names."""

    field_name = "columns"

    def __init__(self) -> None:
        super().__init__([_("One or more columns are duplicated")])


class DatasetColumnsExistsValidationError(DatasetValidationError):
    """Raised when new columns conflict with existing column names on the dataset."""

    field_name = "columns"

    def __init__(self) -> None:
        super().__init__([_("One or more columns already exist")])


class DatasetMetricsNotFoundValidationError(DatasetValidationError):
    """Raised when one or more submitted metric IDs do not exist on the dataset."""

    field_name = "metrics"

    def __init__(self) -> None:
        super().__init__([_("One or more metrics do not exist")])


class DatasetMetricsDuplicateValidationError(DatasetValidationError):
    """Raised when submitted metrics contain duplicate names."""

    field_name = "metrics"

    def __init__(self) -> None:
        super().__init__([_("One or more metrics are duplicated")])


class DatasetMetricsExistsValidationError(DatasetValidationError):
    """Raised when new metrics conflict with existing metric names on the dataset."""

    field_name = "metrics"

    def __init__(self) -> None:
        super().__init__([_("One or more metrics already exist")])


class TableNotFoundValidationError(DatasetValidationError):
    """Raised when the target physical table cannot be found in the database."""

    field_name = "table"

    def __init__(self, table: Table) -> None:
        super().__init__(
            [
                _(
                    "Table [%(table)s] could not be found, "
                    "please double check your "
                    "database connection, schema, and "
                    "table name",
                    table=table,
                )
            ]
        )


class OwnersNotFoundValidationError(DatasetValidationError):
    """Raised when one or more submitted owner IDs do not exist."""

    field_name = "owners"

    def __init__(self) -> None:
        super().__init__([_("Owners are invalid")])


class DatasetDataAccessIsNotAllowed(DatasetValidationError):  # noqa: N818
    """Raised when the user lacks access to the SQL statement's data sources."""

    field_name = "sql"

    def __init__(self, message: str) -> None:
        super().__init__([_(message)])


class DatasourceTypeInvalidError(DatasetValidationError):
    """Raised by the duplicate command to reject non-virtual (physical) datasets."""

    field_name = "datasource_type"

    def __init__(self) -> None:
        super().__init__([_("Datasource type is invalid")])


class DatasetInvalidError(CommandInvalidError):
    """Accumulating dataset validation error.

    Holds a list of per-field :class:`DatasetValidationError` children and
    merges their ``normalized_messages()`` into a ``{field: [messages]}`` dict
    for the per-field 422 response body.
    """

    status_code = 422
    message = _("Dataset parameters are invalid.")

    def __init__(
        self,
        exceptions: list[DatasetValidationError] | None = None,
    ) -> None:
        super().__init__(message=str(self.message))
        self._exceptions: list[DatasetValidationError] = list(  # type: ignore[assignment]
            exceptions or []
        )

    def append(self, exception: DatasetValidationError) -> None:  # type: ignore[override]
        self._exceptions.append(exception)

    def extend(self, exceptions: list[DatasetValidationError]) -> None:  # type: ignore[override]
        self._exceptions.extend(exceptions)

    def normalized_messages(self) -> dict[str, list[str]]:
        errors: dict[str, list[str]] = {}
        for exception in self._exceptions:
            errors.update(exception.normalized_messages())
        return errors


class WarmUpCacheTableNotFoundError(CommandException):
    # Both ``status`` and ``status_code`` kept in sync — different exception
    # handlers key off different fields, both must agree on 404.
    status = 404
    status_code = 404
    message = _("The provided table was not found in the provided database")


class DatasetCreateFailedError(CommandInvalidError):
    status_code = 422
    message = _("Dataset could not be created.")


class DatasetRefreshFailedError(CommandInvalidError):
    status_code = 422
    message = _("Dataset could not be updated.")


class DatasetForbiddenDataURI(ImportFailedError):  # noqa: N818
    message = _("Data URI is not allowed.")


def dataset_invalid_error_handler(request: Any, exc: DatasetInvalidError) -> Any:
    """Litestar exception handler for :class:`DatasetInvalidError`.

    Returns a per-field 422 body ``{"message": {field: [messages]}}`` instead
    of the flat-string ``{"message": "..."}`` shape. Registered only for
    datasets; other commands use the flat-string convention.
    """
    from litestar import Response
    from litestar.enums import MediaType

    return Response(
        content={"message": exc.normalized_messages()},
        status_code=exc.status_code,
        media_type=MediaType.JSON,
    )


__all__ = (
    "CommandInvalidError",
    "DatabaseNotFoundValidationError",
    "DatasetColumnNotFoundValidationError",
    "DatasetColumnsDuplicateValidationError",
    "DatasetColumnsExistsValidationError",
    "DatasetCreateFailedError",
    "DatasetDataAccessIsNotAllowed",
    "DatasetExistsValidationError",
    "DatasetForbiddenDataURI",
    "DatasetInvalidError",
    "DatasetMetricsDuplicateValidationError",
    "DatasetMetricsExistsValidationError",
    "DatasetMetricsNotFoundValidationError",
    "DatasetRefreshFailedError",
    "DatasetValidationError",
    "DatasourceTypeInvalidError",
    "MultiCatalogDisabledValidationError",
    "ObjectNotFoundError",
    "OwnersNotFoundValidationError",
    "TableNotFoundValidationError",
    "WarmUpCacheTableNotFoundError",
    "dataset_invalid_error_handler",
)
