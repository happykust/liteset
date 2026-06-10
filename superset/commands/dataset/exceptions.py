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
"""Dataset-specific exceptions.

The async port re-uses the centralized exceptions from
:mod:`superset.exceptions` and adds dataset-only ones (1:1 with
``superset_old/commands/dataset/exceptions.py``).
"""

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
    # 1:1 with ``superset_old/commands/dataset/exceptions.py:32``.
    return _("Dataset %(table)s already exists", table=table)


class DatasetValidationError(CommandInvalidError):
    """Lightweight per-field dataset validation error.

    Port of the marshmallow ``ValidationError`` subclasses in
    ``superset_old/commands/dataset/exceptions.py``.  Each instance carries a
    ``field_name`` and a list of human-readable messages so an accumulating
    :class:`DatasetInvalidError` can merge them into the ``{field: [messages]}``
    body that upstream FAB returns via
    ``response_422(message=ex.normalized_messages())``.

    The async port does not use marshmallow; ``normalized_messages()`` is
    reimplemented here to produce the exact same shape marshmallow would.
    """

    status_code = 422
    # Marshmallow uses the ``"_schema"`` key for field-less ValidationErrors.
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
    """1:1 with ``superset_old`` ``MultiCatalogDisabledValidationError``."""

    field_name = "catalog"

    def __init__(self) -> None:
        super().__init__(
            [_("Only the default catalog is supported for this connection")]
        )


class DatabaseNotFoundValidationError(DatasetValidationError):
    """1:1 with ``superset_old`` ``DatabaseNotFoundValidationError``."""

    field_name = "database"

    def __init__(self) -> None:
        super().__init__([_("Database does not exist")])


class DatasetExistsValidationError(DatasetValidationError):
    """1:1 with ``superset_old`` ``DatasetExistsValidationError``."""

    field_name = "table"

    def __init__(self, table: Table) -> None:
        super().__init__([get_dataset_exist_error_msg(table)])


class DatasetColumnNotFoundValidationError(DatasetValidationError):
    """1:1 with ``superset_old`` ``DatasetColumnNotFoundValidationError``."""

    field_name = "columns"

    def __init__(self) -> None:
        super().__init__([_("One or more columns do not exist")])


class DatasetColumnsDuplicateValidationError(DatasetValidationError):
    """1:1 with ``superset_old`` ``DatasetColumnsDuplicateValidationError``."""

    field_name = "columns"

    def __init__(self) -> None:
        super().__init__([_("One or more columns are duplicated")])


class DatasetColumnsExistsValidationError(DatasetValidationError):
    """1:1 with ``superset_old`` ``DatasetColumnsExistsValidationError``."""

    field_name = "columns"

    def __init__(self) -> None:
        super().__init__([_("One or more columns already exist")])


class DatasetMetricsNotFoundValidationError(DatasetValidationError):
    """1:1 with ``superset_old`` ``DatasetMetricsNotFoundValidationError``."""

    field_name = "metrics"

    def __init__(self) -> None:
        super().__init__([_("One or more metrics do not exist")])


class DatasetMetricsDuplicateValidationError(DatasetValidationError):
    """1:1 with ``superset_old`` ``DatasetMetricsDuplicateValidationError``."""

    field_name = "metrics"

    def __init__(self) -> None:
        super().__init__([_("One or more metrics are duplicated")])


class DatasetMetricsExistsValidationError(DatasetValidationError):
    """1:1 with ``superset_old`` ``DatasetMetricsExistsValidationError``."""

    field_name = "metrics"

    def __init__(self) -> None:
        super().__init__([_("One or more metrics already exist")])


class TableNotFoundValidationError(DatasetValidationError):
    """1:1 with ``superset_old`` ``TableNotFoundValidationError``."""

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
    """1:1 with ``superset_old`` ``OwnersNotFoundValidationError``."""

    field_name = "owners"

    def __init__(self) -> None:
        super().__init__([_("Owners are invalid")])


class DatasetDataAccessIsNotAllowed(DatasetValidationError):  # noqa: N818
    """1:1 with ``superset_old`` ``DatasetDataAccessIsNotAllowed``."""

    field_name = "sql"

    def __init__(self, message: str) -> None:
        super().__init__([_(message)])


class DatasourceTypeInvalidError(DatasetValidationError):
    """1:1 with ``superset_old`` ``commands/exceptions.py::DatasourceTypeInvalidError``.

    Used by the duplicate command to reject non-virtual datasets.
    """

    field_name = "datasource_type"

    def __init__(self) -> None:
        super().__init__([_("Datasource type is invalid")])


class DatasetInvalidError(CommandInvalidError):
    """Accumulating dataset validation error.

    Port of ``superset_old/commands/dataset/exceptions.py::DatasetInvalidError``
    combined with ``CommandInvalidError.normalized_messages`` from
    ``superset_old/commands/exceptions.py``.  Holds a list of per-field child
    errors (:class:`DatasetValidationError`) and merges their
    ``normalized_messages()`` into a single ``{field: [messages]}`` dict so the
    dataset controller can emit the per-field 422 body 1:1 with upstream FAB
    ``response_422(message=ex.normalized_messages())``.
    """

    status_code = 422
    message = _("Dataset parameters are invalid.")

    def __init__(
        self,
        exceptions: list[DatasetValidationError] | None = None,
    ) -> None:
        # Call super first so CommandInvalidError.__init__ initialises
        # self._exceptions; then overwrite with our typed list so that
        # append/extend/normalized_messages work on the correct collection.
        super().__init__(message=str(self.message))
        self._exceptions: list[DatasetValidationError] = list(  # type: ignore[assignment]
            exceptions or []
        )

    def append(self, exception: DatasetValidationError) -> None:  # type: ignore[override]
        self._exceptions.append(exception)

    def extend(self, exceptions: list[DatasetValidationError]) -> None:  # type: ignore[override]
        self._exceptions.extend(exceptions)

    def normalized_messages(self) -> dict[str, list[str]]:
        # Mirrors upstream ``CommandInvalidError.normalized_messages`` which
        # uses ``dict.update`` (last write wins for a repeated field key).
        errors: dict[str, list[str]] = {}
        for exception in self._exceptions:
            errors.update(exception.normalized_messages())
        return errors


class WarmUpCacheTableNotFoundError(CommandException):
    # ``status`` mirrors ``superset_old/commands/dataset/exceptions.py:205``
    # (1:1 with original).  ``status_code`` is kept in sync so the Liteset
    # exception-to-HTTP mapping (which keys off ``status_code`` everywhere
    # else) still emits the correct 404.
    status = 404
    status_code = 404
    message = _("The provided table was not found in the provided database")


class DatasetCreateFailedError(CommandInvalidError):
    # 1:1 with ``superset_old/commands/dataset/exceptions.py:165``.  The
    # original maps to 422 via the API view's explicit handler; here the
    # ``status_code`` carries that mapping (CommandInvalidError = 422).
    status_code = 422
    message = _("Dataset could not be created.")


class DatasetRefreshFailedError(CommandInvalidError):
    # 1:1 with ``superset_old/commands/dataset/exceptions.py:177`` (which
    # subclasses ``UpdateFailedError`` and surfaces "Dataset could not be
    # updated." → 422).
    status_code = 422
    message = _("Dataset could not be updated.")


class DatasetForbiddenDataURI(ImportFailedError):  # noqa: N818
    # 1:1 with original. Returns 500 when Data URI is forbidden.
    message = _("Data URI is not allowed.")


def dataset_invalid_error_handler(request: Any, exc: DatasetInvalidError) -> Any:
    """Litestar handler for :class:`DatasetInvalidError`.

    Emits the per-field 422 body 1:1 with upstream FAB
    ``response_422(message=ex.normalized_messages())`` — i.e.
    ``{"message": {field: [messages]}}`` — instead of the port-wide
    flat-string ``{"message": "..."}`` shape produced by
    ``superset_exception_handler``.  Registered only for datasets; every other
    command keeps the flat-string convention.
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
