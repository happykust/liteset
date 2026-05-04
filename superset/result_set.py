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
"""Superset wrapper around ``pyarrow.Table``.

Ported 1:1 from ``superset_old/result_set.py``.  The class is the central
piece that bridges raw DB-API cursor data into a normalised pyarrow
table so that downstream code (``sql_lab``, ``models/connectors``,
``db_engine_specs/presto``) can rely on a uniform shape regardless of
the underlying driver's quirks.

Public API:

* :class:`SupersetResultSet`
* :func:`dedup`
* :func:`stringify`
* :func:`stringify_values`
* :func:`destringify`
* :func:`convert_to_string`
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
from numpy.typing import NDArray

from superset.db_engine_specs import BaseEngineSpec
from superset.db_engine_specs.base import ResultSetColumnType
from superset.typing import DbapiDescription, DbapiResult
from superset.utils import core as utils, json
from superset.utils.core import GenericDataType

logger = logging.getLogger(__name__)


def dedup(
    l: list[str],  # noqa: E741
    suffix: str = "__",
    case_sensitive: bool = True,
) -> list[str]:
    """De-duplicate a list of strings by suffixing a counter.

    Always returns the same number of entries as provided, and always
    returns unique values.  Comparison is case-sensitive by default.

    >>> print(','.join(dedup(['foo', 'bar', 'bar', 'bar', 'Bar'])))
    foo,bar,bar__1,bar__2,Bar
    >>> print(
    ...     ','.join(dedup(['foo', 'bar', 'bar', 'bar', 'Bar'], case_sensitive=False))
    ... )
    foo,bar,bar__1,bar__2,Bar__3
    """
    new_l: list[str] = []
    seen: dict[str, int] = {}
    for item in l:
        s_fixed_case = item if case_sensitive else item.lower()
        if s_fixed_case in seen:
            seen[s_fixed_case] += 1
            item += suffix + str(seen[s_fixed_case])
        else:
            seen[s_fixed_case] = 0
        new_l.append(item)
    return new_l


def stringify(obj: Any) -> str:
    """JSON-encode ``obj`` using the legacy ISO-datetime serializer.

    The original Superset used ``json_iso_dttm_ser`` from
    ``superset.utils.json`` which the Liteset port renamed to
    ``_default_serializer``.  We keep the public function name stable
    and delegate to ``json.dumps`` with the module's default serializer.
    """
    return json.dumps(obj)


def stringify_values(array: NDArray[Any]) -> NDArray[Any]:
    """Stringify each entry of ``array`` in place.

    Mirrors ``superset_old.result_set.stringify_values`` byte-for-byte:
    a NaN-aware ``nditer`` walk that first attempts a plain ``str()``
    cast and falls back to JSON encoding via :func:`stringify`.
    """
    result = np.copy(array)

    with np.nditer(result, flags=["refs_ok"], op_flags=[["readwrite"]]) as it:
        for obj in it:
            if na_obj := pd.isna(obj):
                # pandas <NA> type cannot be converted to string
                obj[na_obj] = None
            else:
                try:
                    # for simple string conversions
                    # this handles odd character types better
                    obj[...] = obj.astype(str)
                except ValueError:
                    obj[...] = stringify(obj)

    return result


def destringify(obj: str) -> Any:
    """Decode a JSON-encoded string back into Python types."""
    return json.loads(obj)


def convert_to_string(value: Any) -> str:
    """Coerce ``value`` to ``str``, decoding ``bytes`` as UTF-8.

    Used to ensure column names from the cursor description are strings
    (some DB drivers emit ``bytes`` or other types in the description).
    """
    if isinstance(value, str):
        return value

    if isinstance(value, bytes):
        return value.decode("utf-8")

    return str(value)


class SupersetResultSet:
    """Wrap raw DB-API result data in a uniform :class:`pyarrow.Table`.

    Ported 1:1 from ``superset_old.result_set.SupersetResultSet``.  Used
    by SQL Lab to materialise query results, by Presto/Trino engine specs
    to inspect and serialise rows, and by ``models.connectors`` for the
    legacy datasource path.
    """

    def __init__(  # pylint: disable=too-many-locals  # noqa: C901
        self,
        data: DbapiResult,
        cursor_description: DbapiDescription,
        db_engine_spec: type[BaseEngineSpec],
    ) -> None:
        self.db_engine_spec = db_engine_spec
        data = data or []
        column_names: list[str] = []
        pa_data: list[pa.Array] = []
        deduped_cursor_desc: list[tuple[Any, ...]] = []
        numpy_dtype: list[tuple[str, ...]] = []
        stringified_arr: NDArray[Any]

        if cursor_description:
            # get deduped list of column names
            column_names = dedup(
                [convert_to_string(col[0]) for col in cursor_description]
            )

            # fix cursor descriptor with the deduped names
            deduped_cursor_desc = [
                tuple([column_name, *list(description)[1:]])  # noqa: C409
                for column_name, description in zip(
                    column_names, cursor_description, strict=False
                )
            ]

            # generate numpy structured array dtype
            numpy_dtype = [(column_name, "object") for column_name in column_names]

        # only do expensive recasting if datatype is not standard list of tuples
        if data and (not isinstance(data, list) or not isinstance(data[0], tuple)):
            data = [tuple(row) for row in data]
        array = np.array(data, dtype=numpy_dtype)

        for column in column_names:
            try:
                pa_data.append(pa.array(array[column].tolist()))
            except (
                pa.lib.ArrowInvalid,
                pa.lib.ArrowTypeError,
                pa.lib.ArrowNotImplementedError,
                ValueError,
                TypeError,  # this is super hackey,
                # https://issues.apache.org/jira/browse/ARROW-7855
            ):
                # attempt serialization of values as strings
                stringified_arr = stringify_values(array[column])
                pa_data.append(pa.array(stringified_arr.tolist()))

        if pa_data:  # pylint: disable=too-many-nested-blocks
            for i, column in enumerate(column_names):
                if pa.types.is_nested(pa_data[i].type):
                    # TODO: revisit nested column serialization once nested types
                    #  are added as a natively supported column type in Superset
                    #  (superset.utils.core.GenericDataType).
                    stringified_arr = stringify_values(array[column])
                    pa_data[i] = pa.array(stringified_arr.tolist())

                elif pa.types.is_temporal(pa_data[i].type):
                    # workaround for bug converting
                    # `psycopg2.tz.FixedOffsetTimezone` tzinfo values.
                    # related: https://issues.apache.org/jira/browse/ARROW-5248
                    sample = self.first_nonempty(array[column])
                    if sample and isinstance(sample, datetime.datetime):
                        try:
                            if sample.tzinfo:
                                tz = sample.tzinfo
                                series = pd.Series(array[column])
                                series = pd.to_datetime(series, utc=True)
                                pa_data[i] = pa.Array.from_pandas(
                                    series,
                                    type=pa.timestamp("ns", tz=tz),
                                )
                        except Exception as ex:  # pylint: disable=broad-except
                            logger.exception(ex)

        if not pa_data:
            column_names = []

        self.table = pa.Table.from_arrays(pa_data, names=column_names)
        self._type_dict: dict[str, Any] = {}
        try:
            # The driver may not be passing a cursor.description
            self._type_dict = {
                col: db_engine_spec.get_datatype(deduped_cursor_desc[i][1])
                for i, col in enumerate(column_names)
                if deduped_cursor_desc
            }
        except Exception as ex:  # pylint: disable=broad-except
            logger.exception(ex)

    @staticmethod
    def convert_pa_dtype(pa_dtype: pa.DataType) -> str | None:
        if pa.types.is_boolean(pa_dtype):
            return "BOOL"
        if pa.types.is_integer(pa_dtype):
            return "INT"
        if pa.types.is_floating(pa_dtype):
            return "FLOAT"
        if pa.types.is_string(pa_dtype):
            return "STRING"
        if pa.types.is_temporal(pa_dtype):
            return "DATETIME"
        return None

    @staticmethod
    def convert_table_to_df(table: pa.Table) -> pd.DataFrame:
        try:
            return table.to_pandas(integer_object_nulls=True)
        except pa.lib.ArrowInvalid:
            return table.to_pandas(integer_object_nulls=True, timestamp_as_object=True)

    @staticmethod
    def first_nonempty(items: NDArray[Any]) -> Any:
        return next((i for i in items if i), None)

    def is_temporal(self, db_type_str: str | None) -> bool:
        column_spec = self.db_engine_spec.get_column_spec(db_type_str)
        if column_spec is None:
            return False
        return column_spec.is_dttm

    def type_generic(self, db_type_str: str | None) -> utils.GenericDataType | None:
        column_spec = self.db_engine_spec.get_column_spec(db_type_str)
        if column_spec is None:
            return None

        if column_spec.is_dttm:
            return GenericDataType.TEMPORAL

        return column_spec.generic_type

    def data_type(self, col_name: str, pa_dtype: pa.DataType) -> str | None:
        """Given a pyarrow data type, return a generic database type string."""
        if set_type := self._type_dict.get(col_name):
            return set_type

        if mapped_type := self.convert_pa_dtype(pa_dtype):
            return mapped_type

        return None

    def to_pandas_df(self) -> pd.DataFrame:
        return self.convert_table_to_df(self.table)

    @property
    def pa_table(self) -> pa.Table:
        return self.table

    @property
    def size(self) -> int:
        return self.table.num_rows

    @property
    def columns(self) -> list[ResultSetColumnType]:
        if not self.table.column_names:
            return []

        columns: list[ResultSetColumnType] = []
        for col in self.table.schema:
            db_type_str = self.data_type(col.name, col.type)
            column: ResultSetColumnType = {
                "column_name": col.name,
                "name": col.name,
                "type": db_type_str,
                "type_generic": self.type_generic(db_type_str),
                "is_dttm": self.is_temporal(db_type_str) or False,
            }
            columns.append(column)
        return columns
