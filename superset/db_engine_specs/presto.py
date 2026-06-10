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
"""Presto engine spec -- sync/Flask-compatible.

Ported 1:1 from ``superset_old/db_engine_specs/presto.py`` with Flask
imports removed.  The abstract ``PrestoBaseEngineSpec`` (shared with Trino)
lives in ``superset/db_engine_specs/trino.py``; the concrete
``PrestoEngineSpec`` lives here and carries the Presto-only partition /
column-introspection / expand-data behaviour that ``HiveEngineSpec`` also
relies on (Hive subclasses ``PrestoEngineSpec``).

Flask ``g``/``app.config`` are replaced with the port's established
equivalents:

* feature flags -> ``feature_flag_manager.is_feature_enabled``
* ``database.get_raw_connection`` (not ported as a ``Database`` method) ->
  a local ``_raw_connection`` helper built on ``get_sync_engine`` that
  mirrors the original (prequeries + ``engine.raw_connection()``).
* ``cache_manager.data_cache.memoize`` -> ``cache_manager.sync_cache``
  manual get/set, matching the ClickHouse port's ``get_function_names``.
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from collections import defaultdict, deque
from contextlib import closing, contextmanager
from re import Pattern
from textwrap import dedent
from typing import Any, cast, Iterator, Optional, TYPE_CHECKING

from packaging.version import Version
from sqlalchemy import Column, literal_column, types
from sqlalchemy.engine.base import Engine
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.engine.result import Row as ResultRow
from sqlalchemy.engine.url import URL
from sqlalchemy.exc import NoSuchTableError
from sqlalchemy.sql.expression import ColumnClause, Select

from superset.common.query_status import QueryStatus
from superset.db_engine_specs.base import ResultSetColumnType
from superset.db_engine_specs.exceptions import SupersetDBAPIProgrammingError
from superset.db_engine_specs.trino import (
    CONNECTION_ACCESS_DENIED_REGEX,
    CONNECTION_HOST_DOWN_REGEX,
    CONNECTION_INVALID_HOSTNAME_REGEX,
    CONNECTION_PORT_CLOSED_REGEX,
    CONNECTION_UNKNOWN_DATABASE_ERROR,
    PrestoBaseEngineSpec,
)
from superset.exceptions import SupersetTemplateException
from superset.result_set import destringify
from superset.utils import core as utils, json
from superset.utils.feature_flags import feature_flag_manager

if TYPE_CHECKING:
    from superset.models.core import Database
    from superset.models.sql_lab import Query
    from superset.sql.parse import Table

    with contextlib.suppress(ImportError):  # pyhive may not be installed
        from pyhive.presto import Cursor

logger = logging.getLogger(__name__)


def is_feature_enabled(feature: str) -> bool:
    """Port shim for upstream's ``superset.is_feature_enabled``."""
    return feature_flag_manager.is_feature_enabled(feature)


# ---------------------------------------------------------------------------
# Custom Presto SQL types used by ``where_latest_partition`` — the port's
# ``trino.py`` defines the column-mapping types (Array/Map/Row/Interval/
# TinyInteger) but not these two date/timestamp markers.
# ---------------------------------------------------------------------------


class Date(types.TypeEngine):  # pylint: disable=abstract-method
    """Presto ``date`` partition-value marker type."""


class TimeStamp(types.TypeEngine):  # pylint: disable=abstract-method
    """Presto ``timestamp`` partition-value marker type."""


# ---------------------------------------------------------------------------
# Error regexes (Presto-only; the connection regexes are shared with Trino)
# ---------------------------------------------------------------------------

COLUMN_DOES_NOT_EXIST_REGEX = re.compile(
    "line (?P<location>.+?): .*Column '(?P<column_name>.+?)' cannot be resolved"
)
TABLE_DOES_NOT_EXIST_REGEX = re.compile(".*Table (?P<table_name>.+?) does not exist")
SCHEMA_DOES_NOT_EXIST_REGEX = re.compile(
    "line (?P<location>.+?): .*Schema '(?P<schema_name>.+?)' does not exist"
)


def split(
    string: str, delimiter: str = " ", quote: str = '"', escaped_quote: str = r"\""
) -> Iterator[str]:
    """A split function that is aware of quotes and parentheses.

    Ported locally from ``superset_old/utils/core.py:split`` — the port's
    ``superset.utils.core`` does not expose ``split``; ``get_children``
    (Presto ROW parsing) is its only caller.

    :param string: string to split
    :param delimiter: string defining where to split, usually a comma or space
    :param quote: string, either a single or a double quote
    :param escaped_quote: string representing an escaped quote
    :return: list of strings
    """
    parens = 0
    quotes = False
    i = 0
    for j, character in enumerate(string):
        complete = parens == 0 and not quotes
        if complete and character == delimiter:
            yield string[i:j]
            i = j + len(delimiter)
        elif character == "(":
            parens += 1
        elif character == ")":
            parens -= 1
        elif character == quote:
            if quotes and string[j - len(escaped_quote) + 1 : j + 1] != escaped_quote:
                quotes = False
            elif not quotes:
                quotes = True
    yield string[i:]


def get_children(column: ResultSetColumnType) -> list[ResultSetColumnType]:
    """
    Get the children of a complex Presto type (row or array).

    For arrays, we return a single list with the base type:

        >>> get_children(dict(name="a", type="ARRAY(BIGINT)", is_dttm=False))
        [{"name": "a", "type": "BIGINT", "is_dttm": False}]

    For rows, we return a list of the columns:

        >>> get_children(dict(name="a", type="ROW(BIGINT,FOO VARCHAR)",  is_dttm=False))
        [{'name': 'a._col0', 'type': 'BIGINT', 'is_dttm': False}, {'name': 'a.foo', 'type': 'VARCHAR', 'is_dttm': False}]  # pylint: disable=line-too-long

    :param column: dictionary representing a Presto column
    :return: list of dictionaries representing children columns
    """  # noqa: E501
    pattern = re.compile(r"(?P<type>\w+)\((?P<children>.*)\)")
    if not column["type"]:
        raise ValueError
    match = pattern.match(cast(str, column["type"]))
    if not match:
        raise Exception(  # pylint: disable=broad-exception-raised
            f"Unable to parse column type {column['type']}"
        )

    group = match.groupdict()
    type_ = group["type"].upper()
    children_type = group["children"]
    if type_ == "ARRAY":
        return [
            {
                "column_name": column["column_name"],
                "name": column["column_name"],
                "type": children_type,
                "is_dttm": False,
            }
        ]

    if type_ == "ROW":
        nameless_columns = 0
        columns = []
        for child in split(children_type, ","):
            parts = list(split(child.strip(), " "))
            if len(parts) == 2:
                name, type_ = parts
                name = name.strip('"')
            else:
                name = f"_col{nameless_columns}"
                type_ = parts[0]
                nameless_columns += 1
            _column: ResultSetColumnType = {
                "column_name": f"{column['column_name']}.{name.lower()}",
                "name": f"{column['column_name']}.{name.lower()}",
                "type": type_,
                "is_dttm": False,
            }
            columns.append(_column)
        return columns

    raise Exception(f"Unknown type {type_}!")  # pylint: disable=broad-exception-raised


@contextmanager
def _raw_connection(
    database: Database,
    catalog: str | None = None,
    schema: str | None = None,
) -> Iterator[Any]:
    """Yield a raw DB-API connection with the engine-spec prequeries applied.

    Port equivalent of the original ``Database.get_raw_connection`` (which is
    not ported as a ``Database`` method): open a sync engine via
    ``get_sync_engine``, take a raw connection, and run the engine spec's
    prequeries (used to select the catalog/schema) before yielding.
    """
    from superset.utils.database import get_sync_engine

    with get_sync_engine(database, catalog=catalog, schema=schema) as engine:
        with closing(engine.raw_connection()) as conn:
            for prequery in database.db_engine_spec.get_prequeries(
                database=database,
                catalog=catalog,
                schema=schema,
            ):
                cursor = conn.cursor()
                cursor.execute(prequery)
            yield conn


class PrestoEngineSpec(PrestoBaseEngineSpec):
    engine = "presto"
    engine_name = "Presto"
    allows_alias_to_source_column = False

    custom_errors: dict[Pattern[str], tuple[str, str, dict[str, Any]]] = {
        COLUMN_DOES_NOT_EXIST_REGEX: (
            'We can\'t seem to resolve the column "%(column_name)s" at '
            "line %(location)s.",
            "COLUMN_DOES_NOT_EXIST_ERROR",
            {},
        ),
        TABLE_DOES_NOT_EXIST_REGEX: (
            'The table "%(table_name)s" does not exist. '
            "A valid table must be used to run this query.",
            "TABLE_DOES_NOT_EXIST_ERROR",
            {},
        ),
        SCHEMA_DOES_NOT_EXIST_REGEX: (
            'The schema "%(schema_name)s" does not exist. '
            "A valid schema must be used to run this query.",
            "SCHEMA_DOES_NOT_EXIST_ERROR",
            {},
        ),
        CONNECTION_ACCESS_DENIED_REGEX: (
            'Either the username "%(username)s" or the password is incorrect.',
            "CONNECTION_ACCESS_DENIED_ERROR",
            {},
        ),
        CONNECTION_INVALID_HOSTNAME_REGEX: (
            'The hostname "%(hostname)s" cannot be resolved.',
            "CONNECTION_INVALID_HOSTNAME_ERROR",
            {},
        ),
        CONNECTION_HOST_DOWN_REGEX: (
            'The host "%(hostname)s" might be down, and can\'t be '
            "reached on port %(port)s.",
            "CONNECTION_HOST_DOWN_ERROR",
            {},
        ),
        CONNECTION_PORT_CLOSED_REGEX: (
            'Port %(port)s on hostname "%(hostname)s" refused the connection.',
            "CONNECTION_PORT_CLOSED_ERROR",
            {},
        ),
        CONNECTION_UNKNOWN_DATABASE_ERROR: (
            'Unable to connect to catalog named "%(catalog_name)s".',
            "CONNECTION_UNKNOWN_DATABASE_ERROR",
            {},
        ),
    }

    # ------------------------------------------------------------------
    # Partition introspection (originally on ``PrestoBaseEngineSpec``)
    # ------------------------------------------------------------------

    @classmethod
    def _partition_query(  # pylint: disable=too-many-arguments,too-many-locals,unused-argument
        cls,
        table: Table,
        indexes: list[dict[str, Any]],
        database: Database,
        limit: int = 0,
        order_by: list[tuple[str, bool]] | None = None,
        filters: dict[Any, Any] | None = None,
    ) -> str:
        """
        Return a partition query.

        Note the unused arguments are exposed for sub-classing purposes where custom
        integrations may require the schema, indexes, etc. to build the partition query.

        :param table: the table instance
        :param indexes: the indexes associated with the table
        :param database: the database the query will be run against
        :param limit: the number of partitions to be returned
        :param order_by: a list of tuples of field name and a boolean
            that determines if that field should be sorted in descending
            order
        :param filters: dict of field name and filter value combinations
        """
        limit_clause = f"LIMIT {limit}" if limit else ""
        order_by_clause = ""
        if order_by:
            l = []  # noqa: E741
            for field, desc in order_by:
                l.append(field + " DESC" if desc else "")
            order_by_clause = "ORDER BY " + ", ".join(l)

        where_clause = ""
        if filters:
            l = []  # noqa: E741
            for field, value in filters.items():
                l.append(f"{field} = '{value}'")
            where_clause = "WHERE " + " AND ".join(l)

        # Partition select syntax changed in v0.199, so check here.
        # Default to the new syntax if version is unset.
        presto_version = database.get_extra().get("version")

        if presto_version and Version(presto_version) < Version("0.199"):
            full_table_name = (
                f"{table.schema}.{table.table}" if table.schema else table.table
            )
            partition_select_clause = f"SHOW PARTITIONS FROM {full_table_name}"
        else:
            system_table_name = f'"{table.table}$partitions"'
            full_table_name = (
                f"{table.schema}.{system_table_name}"
                if table.schema
                else system_table_name
            )
            partition_select_clause = f"SELECT * FROM {full_table_name}"  # noqa: S608

        sql = dedent(
            f"""\
            {partition_select_clause}
            {where_clause}
            {order_by_clause}
            {limit_clause}
        """
        )
        return sql

    @classmethod
    def where_latest_partition(
        cls,
        database: Database,
        table: Table,
        query: Select,
        columns: list[ResultSetColumnType] | None = None,
    ) -> Select | None:
        try:
            col_names, values = cls.latest_partition(database, table, show_first=True)
        except Exception:  # pylint: disable=broad-except
            # table is not partitioned
            return None

        if values is None:
            return None

        column_type_by_name = {
            column.get("column_name"): column.get("type") for column in columns or []
        }

        for col_name, value in zip(col_names, values, strict=False):
            col_type = column_type_by_name.get(col_name)

            if isinstance(col_type, str):
                col_type_class = getattr(types, col_type, None)
                col_type = col_type_class() if col_type_class else None

            if isinstance(col_type, types.DATE):
                col_type = Date()
            elif isinstance(col_type, types.TIMESTAMP):
                col_type = TimeStamp()

            query = query.where(Column(col_name, col_type) == value)

        return query

    @classmethod
    def _latest_partition_from_df(cls, df: Any) -> list[str] | None:
        if not df.empty:
            return df.to_records(index=False)[0].item()
        return None

    @classmethod
    def latest_partition(
        cls,
        database: Database,
        table: Table,
        show_first: bool = False,
        indexes: list[dict[str, Any]] | None = None,
    ) -> tuple[list[str], list[str] | None]:
        """Returns col name and the latest (max) partition value for a table

        :param table: the table instance
        :param database: database query will be run against
        :type database: models.Database
        :param show_first: displays the value for the first partitioning key
          if there are many partitioning keys
        :param indexes: indexes from the database
        :type show_first: bool

        >>> latest_partition('foo_table')
        (['ds'], ('2018-01-01',))
        """
        if indexes is None:
            with database.get_inspector(
                catalog=table.catalog,
                schema=table.schema,
            ) as inspector:
                indexes = cls.get_indexes(database, inspector, table)

        if not indexes:
            raise SupersetTemplateException(
                f"Error getting partition for {table}. "
                "Verify that this table has a partition."
            )

        if len(indexes[0]["column_names"]) < 1:
            raise SupersetTemplateException(
                "The table should have one partitioned field"
            )

        if not show_first and len(indexes[0]["column_names"]) > 1:
            raise SupersetTemplateException(
                "The table should have a single partitioned field "
                "to use this function. You may want to use "
                "`presto.latest_sub_partition`"
            )

        column_names = indexes[0]["column_names"]

        return column_names, cls._latest_partition_from_df(
            df=database.get_df(
                sql=cls._partition_query(
                    table,
                    indexes,
                    database,
                    limit=1,
                    order_by=[(column_name, True) for column_name in column_names],
                ),
                catalog=table.catalog,
                schema=table.schema,
            )
        )

    @classmethod
    def latest_sub_partition(
        cls,
        database: Database,
        table: Table,
        **kwargs: Any,
    ) -> Any:
        """Returns the latest (max) partition value for a table

        A filtering criteria should be passed for all fields that are
        partitioned except for the field to be returned. For example,
        if a table is partitioned by (``ds``, ``event_type`` and
        ``event_category``) and you want the latest ``ds``, you'll want
        to provide a filter as keyword arguments for both
        ``event_type`` and ``event_category`` as in
        ``latest_sub_partition('my_table',
            event_category='page', event_type='click')``

        :param database: database query will be run against
        :param table: the table instance
        :type table: Table
        :type database: models.Database

        :param kwargs: keyword arguments define the filtering criteria
            on the partition list. There can be many of these.
        :type kwargs: str
        >>> latest_sub_partition('sub_partition_table', event_type='click')
        '2018-01-01'
        """
        with database.get_inspector(
            catalog=table.catalog,
            schema=table.schema,
        ) as inspector:
            indexes = cls.get_indexes(database, inspector, table)
        part_fields = indexes[0]["column_names"]
        for k in kwargs.keys():  # pylint: disable=consider-iterating-dictionary
            if k not in k in part_fields:  # pylint: disable=comparison-with-itself
                msg = f"Field [{k}] is not part of the portioning key"
                raise SupersetTemplateException(msg)
        if len(kwargs.keys()) != len(part_fields) - 1:
            # pylint: disable=consider-using-f-string
            msg = (
                "A filter needs to be specified for {} out of the {} fields."
            ).format(len(part_fields) - 1, len(part_fields))
            raise SupersetTemplateException(msg)

        for field in part_fields:
            if field not in kwargs:
                field_to_return = field

        sql = cls._partition_query(
            table,
            indexes,
            database,
            limit=1,
            order_by=[(field_to_return, True)],
            filters=kwargs,
        )
        df = database.get_df(sql, table.catalog, table.schema)
        if df.empty:
            return ""
        return df.to_dict()[field_to_return][0]

    # ------------------------------------------------------------------
    # Column introspection (originally on ``PrestoBaseEngineSpec``)
    # ------------------------------------------------------------------

    @classmethod
    def _show_columns(
        cls,
        inspector: Inspector,
        table: Table,
    ) -> list[ResultRow]:
        """
        Show presto column names
        :param inspector: object that performs database schema inspection
        :param table: table instance
        :return: list of column objects
        """
        full_table_name = cls.quote_table(table, inspector.engine.dialect)
        return inspector.bind.execute(f"SHOW COLUMNS FROM {full_table_name}").fetchall()

    @classmethod
    def _create_column_info(
        cls,
        name: str,
        data_type: types.TypeEngine,
    ) -> ResultSetColumnType:
        """
        Create column info object
        :param name: column name
        :param data_type: column data type
        :return: column info object
        """
        return {
            "column_name": name,
            "name": name,
            "type": data_type,
            "is_dttm": None,
            "type_generic": None,
        }

    @classmethod
    def get_columns(
        cls,
        inspector: Inspector,
        table: Table,
        options: dict[str, Any] | None = None,
    ) -> list[ResultSetColumnType]:
        """
        Get columns from a Presto data source. This includes handling row and
        array data types
        :param inspector: object that performs database schema inspection
        :param table: table instance
        :param options: Extra configuration options, not used by this backend
        :return: a list of results that contain column info
                (i.e. column name and data type)
        """
        columns = cls._show_columns(inspector, table)
        result: list[ResultSetColumnType] = []
        for column in columns:
            # parse column if it is a row or array
            if is_feature_enabled("PRESTO_EXPAND_DATA") and (
                "array" in column.Type or "row" in column.Type
            ):
                structural_column_index = len(result)
                cls._parse_structural_column(column.Column, column.Type, result)
                result[structural_column_index]["nullable"] = getattr(
                    column, "Null", True
                )
                result[structural_column_index]["default"] = None
                continue

            # otherwise column is a basic data type
            column_spec = cls.get_column_spec(column.Type)
            column_type = column_spec.sqla_type if column_spec else None
            if column_type is None:
                column_type = types.String()
                logger.info(
                    "Did not recognize type %s of column %s",
                    str(column.Type),
                    str(column.Column),
                )
            column_info = cls._create_column_info(column.Column, column_type)
            column_info["nullable"] = getattr(column, "Null", True)
            column_info["default"] = None
            column_info["column_name"] = column.Column
            result.append(column_info)

        return result

    @classmethod
    def _parse_structural_column(  # pylint: disable=too-many-locals  # noqa: C901
        cls,
        parent_column_name: str,
        parent_data_type: str,
        result: list[ResultSetColumnType],
    ) -> None:
        """
        Parse a row or array column
        :param result: list tracking the results
        """
        formatted_parent_column_name = parent_column_name
        # Quote the column name if there is a space
        if " " in parent_column_name:
            formatted_parent_column_name = f'"{parent_column_name}"'
        full_data_type = f"{formatted_parent_column_name} {parent_data_type}"
        original_result_len = len(result)
        # split on open parenthesis ( to get the structural
        # data type and its component types
        data_types = cls._split_data_type(full_data_type, r"\(")
        stack: list[tuple[str, str]] = []
        for data_type in data_types:
            # split on closed parenthesis ) to track which component
            # types belong to what structural data type
            inner_types = cls._split_data_type(data_type, r"\)")
            for inner_type in inner_types:
                # We have finished parsing multiple structural data types
                if not inner_type and stack:
                    stack.pop()
                elif cls._has_nested_data_types(inner_type):
                    # split on comma , to get individual data types
                    single_fields = cls._split_data_type(inner_type, ",")
                    for single_field in single_fields:
                        single_field = single_field.strip()
                        # If component type starts with a comma, the first single field
                        # will be an empty string. Disregard this empty string.
                        if not single_field:
                            continue
                        # split on whitespace to get field name and data type
                        field_info = cls._split_data_type(single_field, r"\s")
                        # check if there is a structural data type within
                        # overall structural data type
                        column_spec = cls.get_column_spec(field_info[1])
                        column_type = column_spec.sqla_type if column_spec else None
                        if column_type is None:
                            column_type = types.String()
                            logger.info(
                                "Did not recognize type %s of column %s",
                                field_info[1],
                                field_info[0],
                            )
                        if field_info[1] == "array" or field_info[1] == "row":
                            stack.append((field_info[0], field_info[1]))
                            full_parent_path = cls._get_full_name(stack)
                            result.append(
                                cls._create_column_info(full_parent_path, column_type)
                            )
                        else:  # otherwise this field is a basic data type
                            full_parent_path = cls._get_full_name(stack)
                            column_name = f"{full_parent_path}.{field_info[0]}"
                            result.append(
                                cls._create_column_info(column_name, column_type)
                            )
                    # If the component type ends with a structural data type, do not pop
                    # the stack. We have run across a structural data type within the
                    # overall structural data type. Otherwise, we have completely parsed
                    # through the entire structural data type and can move on.
                    if not (inner_type.endswith("array") or inner_type.endswith("row")):
                        stack.pop()
                # We have an array of row objects (i.e. array(row(...)))
                elif inner_type in ("array", "row"):
                    # Push a dummy object to represent the structural data type
                    stack.append(("", inner_type))
                # We have an array of a basic data types(i.e. array(varchar)).
                elif stack:
                    # Because it is an array of a basic data type. We have finished
                    # parsing the structural data type and can move on.
                    stack.pop()
        # Unquote the column name if necessary
        if formatted_parent_column_name != parent_column_name:
            for index in range(original_result_len, len(result)):
                result[index]["column_name"] = result[index]["column_name"].replace(
                    formatted_parent_column_name, parent_column_name
                )

    @classmethod
    def _split_data_type(cls, data_type: str, delimiter: str) -> list[str]:
        """
        Split data type based on given delimiter. Do not split the string if the
        delimiter is enclosed in quotes
        :param data_type: data type
        :param delimiter: string separator (i.e. open parenthesis, closed parenthesis,
               comma, whitespace)
        :return: list of strings after breaking it by the delimiter
        """
        return re.split(rf"{delimiter}(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", data_type)

    @classmethod
    def _has_nested_data_types(cls, component_type: str) -> bool:
        """
        Check if string contains a data type. We determine if there is a data type by
        whitespace or multiple data types by commas
        :param component_type: data type
        :return: boolean
        """
        comma_regex = r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)"
        white_space_regex = r"\s(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)"
        return (
            re.search(comma_regex, component_type) is not None
            or re.search(white_space_regex, component_type) is not None
        )

    @classmethod
    def _get_full_name(cls, names: list[tuple[str, str]]) -> str:
        """
        Get the full column name
        :param names: list of all individual column names
        :return: full column name
        """
        return ".".join(column[0] for column in names if column[0])

    # ------------------------------------------------------------------
    # Cost estimation + function names (originally on ``PrestoBaseEngineSpec``)
    # ------------------------------------------------------------------

    @classmethod
    def estimate_statement_cost(
        cls, database: Database, statement: str, cursor: Any
    ) -> dict[str, Any]:
        """
        Run a SQL query that estimates the cost of a given statement.
        :param database: A Database object
        :param statement: A single SQL statement
        :param cursor: Cursor instance
        :return: JSON response from Trino
        """
        sql = f"EXPLAIN (TYPE IO, FORMAT JSON) {statement}"
        cursor.execute(sql)

        # the output from Trino is a single column and a single row containing
        # JSON:
        #
        #   {
        #     ...
        #     "estimate" : {
        #       "outputRowCount" : 8.73265878E8,
        #       "outputSizeInBytes" : 3.41425774958E11,
        #       "cpuCost" : 3.41425774958E11,
        #       "maxMemory" : 0.0,
        #       "networkCost" : 3.41425774958E11
        #     }
        #   }
        result = json.loads(cursor.fetchone()[0])
        return result

    @classmethod
    def query_cost_formatter(
        cls, raw_cost: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        """
        Format cost estimate.
        :param raw_cost: JSON estimate from Trino
        :return: Human readable cost estimate
        """

        def humanize(value: Any, suffix: str) -> str:
            try:
                value = int(value)
            except ValueError:
                return str(value)

            prefixes = ["K", "M", "G", "T", "P", "E", "Z", "Y"]
            prefix = ""
            to_next_prefix = 1000
            while value > to_next_prefix and prefixes:
                prefix = prefixes.pop(0)
                value //= to_next_prefix

            return f"{value} {prefix}{suffix}"

        cost = []
        columns = [
            ("outputRowCount", "Output count", " rows"),
            ("outputSizeInBytes", "Output size", "B"),
            ("cpuCost", "CPU cost", ""),
            ("maxMemory", "Max memory", "B"),
            ("networkCost", "Network cost", ""),
        ]
        for row in raw_cost:
            estimate: dict[str, float] = row.get("estimate", {})
            statement_cost = {}
            for key, label, suffix in columns:
                if key in estimate:
                    statement_cost[label] = humanize(estimate[key], suffix).strip()
            cost.append(statement_cost)

        return cost

    @classmethod
    def get_function_names(cls, database: Database) -> list[str]:
        """
        Get a list of function names that are able to be called on the database.
        Used for SQL Lab autocomplete.

        Results are cached per-database for SQL Lab autocomplete — the business
        equivalent of upstream's ``@cache_manager.data_cache.memoize()``. The
        port's cache layer exposes no flask-caching ``memoize``, so this uses
        the sync cache slot (``cache_manager.sync_cache``) directly with a
        manual get/set, matching the ClickHouse port's ``get_function_names``.

        :param database: The database to get functions for
        :return: A list of function names useable in the database
        """
        from superset.extensions import cache_manager

        cache_key = f"db:{getattr(database, 'id', None)}:function_names"
        try:
            cached = cache_manager.sync_cache.get(cache_key)
            if cached is not None:
                return cached
        except Exception:  # noqa: BLE001
            logger.debug("function-name cache read failed", exc_info=True)

        names = database.get_df("SHOW FUNCTIONS")["Function"].tolist()

        try:
            cache_manager.sync_cache.set(cache_key, names)
        except Exception:  # noqa: BLE001
            logger.debug("function-name cache write failed", exc_info=True)
        return names

    # ------------------------------------------------------------------
    # Presto-only overrides
    # ------------------------------------------------------------------

    @classmethod
    def get_allow_cost_estimate(cls, extra: dict[str, Any]) -> bool:
        version = extra.get("version")
        return version is not None and Version(version) >= Version("0.319")

    @classmethod
    def impersonate_user(
        cls,
        database: Database,
        username: str | None,
        user_token: str | None,
        url: URL,
        engine_kwargs: dict[str, Any],
    ) -> tuple[URL, dict[str, Any]]:
        if username is None:
            return url, engine_kwargs

        url = url.set(username=username)

        backend_name = url.get_backend_name()
        connect_args = engine_kwargs.setdefault("connect_args", {})
        if backend_name == "presto":
            connect_args["principal_username"] = username

        return url, engine_kwargs

    @classmethod
    def get_table_names(
        cls,
        database: Database,
        inspector: Inspector,
        schema: str | None,
    ) -> set[str]:
        """
        Get all the real table names within the specified schema.

        Per the SQLAlchemy definition if the schema is omitted the database’s default
        schema is used, however some dialects infer the request as schema agnostic.

        Note that PyHive's Hive and Presto SQLAlchemy dialects do not adhere to the
        specification where the `get_table_names` method returns both real tables and
        views. Futhermore the dialects wrongfully infer the request as schema agnostic
        when the schema is omitted.

        :param database: The database to inspect
        :param inspector: The SQLAlchemy inspector
        :param schema: The schema to inspect
        :returns: The physical table names
        """

        return super().get_table_names(
            database, inspector, schema
        ) - cls.get_view_names(database, inspector, schema)

    @classmethod
    def get_view_names(
        cls,
        database: Database,
        inspector: Inspector,
        schema: str | None,
    ) -> set[str]:
        """
        Get all the view names within the specified schema.

        Per the SQLAlchemy definition if the schema is omitted the database’s default
        schema is used, however some dialects infer the request as schema agnostic.

        Note that PyHive's Presto SQLAlchemy dialect does not adhere to the
        specification as the `get_view_names` method is not defined. Futhermore the
        dialect wrongfully infers the request as schema agnostic when the schema is
        omitted.

        :param database: The database to inspect
        :param inspector: The SQLAlchemy inspector
        :param schema: The schema to inspect
        :returns: The view names
        """

        if schema:
            sql = dedent(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = %(schema)s
                AND table_type = 'VIEW'
                """
            ).strip()
            params = {"schema": schema}
        else:
            sql = dedent(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_type = 'VIEW'
                """
            ).strip()
            params = {}

        with _raw_connection(database, schema=schema) as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            results = cursor.fetchall()
            return {row[0] for row in results}

    @classmethod
    def _is_column_name_quoted(cls, column_name: str) -> bool:
        """
        Check if column name is in quotes
        :param column_name: column name
        :return: boolean
        """
        return column_name.startswith('"') and column_name.endswith('"')

    @classmethod
    def _get_fields(cls, cols: list[ResultSetColumnType]) -> list[ColumnClause]:
        """
        Format column clauses where names are in quotes and labels are specified
        :param cols: columns
        :return: column clauses
        """
        column_clauses = []
        # Column names are separated by periods. This regex will find periods in a
        # string if they are not enclosed in quotes because if a period is enclosed in
        # quotes, then that period is part of a column name.
        dot_pattern = r"""\.                # split on period
                          (?=               # look ahead
                          (?:               # create non-capture group
                          [^\"]*\"[^\"]*\"  # two quotes
                          )*[^\"]*$)        # end regex"""
        dot_regex = re.compile(dot_pattern, re.VERBOSE)
        for col in cols:
            # get individual column names
            col_names = re.split(dot_regex, col["column_name"])
            # quote each column name if it is not already quoted
            for index, col_name in enumerate(col_names):
                if not cls._is_column_name_quoted(col_name):
                    col_names[index] = f'"{col_name}"'
            quoted_col_name = ".".join(
                col_name if cls._is_column_name_quoted(col_name) else f'"{col_name}"'
                for col_name in col_names
            )
            # create column clause in the format "name"."name" AS "name.name"
            column_clause = literal_column(quoted_col_name).label(col["column_name"])
            column_clauses.append(column_clause)
        return column_clauses

    @classmethod
    def select_star(  # pylint: disable=too-many-arguments
        cls,
        database: Database,
        table: Table,
        engine: Engine,
        limit: int = 100,
        show_cols: bool = False,
        indent: bool = True,
        latest_partition: bool = True,
        cols: list[ResultSetColumnType] | None = None,
    ) -> str:
        """
        Include selecting properties of row objects. We cannot easily break arrays into
        rows, so render the whole array in its own row and skip columns that correspond
        to an array's contents.
        """
        cols = cols or []
        presto_cols = cols
        if is_feature_enabled("PRESTO_EXPAND_DATA") and show_cols:
            dot_regex = r"\.(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)"
            presto_cols = [
                col
                for col in presto_cols
                if not re.search(dot_regex, col["column_name"])
            ]
        return super().select_star(
            database,
            table,
            engine,
            limit,
            show_cols,
            indent,
            latest_partition,
            presto_cols,
        )

    @classmethod
    def expand_data(  # pylint: disable=too-many-locals  # noqa: C901
        cls, columns: list[ResultSetColumnType], data: list[dict[Any, Any]]
    ) -> tuple[
        list[ResultSetColumnType], list[dict[Any, Any]], list[ResultSetColumnType]
    ]:
        """
        We do not immediately display rows and arrays clearly in the data grid. This
        method separates out nested fields and data values to help clearly display
        structural columns.

        Example: ColumnA is a row(nested_obj varchar) and ColumnB is an array(int)
        Original data set = [
            {'ColumnA': ['a1'], 'ColumnB': [1, 2]},
            {'ColumnA': ['a2'], 'ColumnB': [3, 4]},
        ]
        Expanded data set = [
            {'ColumnA': ['a1'], 'ColumnA.nested_obj': 'a1', 'ColumnB': 1},
            {'ColumnA': '',     'ColumnA.nested_obj': '',   'ColumnB': 2},
            {'ColumnA': ['a2'], 'ColumnA.nested_obj': 'a2', 'ColumnB': 3},
            {'ColumnA': '',     'ColumnA.nested_obj': '',   'ColumnB': 4},
        ]
        :param columns: columns selected in the query
        :param data: original data set
        :return: list of all columns(selected columns and their nested fields),
                 expanded data set, listed of nested fields
        """
        if not is_feature_enabled("PRESTO_EXPAND_DATA"):
            return columns, data, []

        # process each column, unnesting ARRAY types and
        # expanding ROW types into new columns
        to_process = deque((column, 0) for column in columns)
        all_columns: list[ResultSetColumnType] = []
        expanded_columns = []
        current_array_level = None
        while to_process:
            column, level = to_process.popleft()
            if column["column_name"] not in [
                column["column_name"] for column in all_columns
            ]:
                all_columns.append(column)

            # When unnesting arrays we need to keep track of how many extra rows
            # were added, for each original row. This is necessary when we expand
            # multiple arrays, so that the arrays after the first reuse the rows
            # added by the first. every time we change a level in the nested arrays
            # we reinitialize this.
            if level != current_array_level:
                unnested_rows: dict[int, int] = defaultdict(int)
                current_array_level = level

            name = column["column_name"]
            values: str | list[Any] | None

            if column["type"] and column["type"].startswith("ARRAY("):
                # keep processing array children; we append to the right so that
                # multiple nested arrays are processed breadth-first
                to_process.append((get_children(column)[0], level + 1))

                # unnest array objects data into new rows
                i = 0
                while i < len(data):
                    row = data[i]
                    values = row.get(name)
                    if isinstance(values, str):
                        row[name] = values = destringify(values)
                    if values:
                        # how many extra rows we need to unnest the data?
                        extra_rows = len(values) - 1

                        # how many rows were already added for this row?
                        current_unnested_rows = unnested_rows[i]

                        # add any necessary rows
                        missing = extra_rows - current_unnested_rows
                        for _ in range(missing):
                            data.insert(i + current_unnested_rows + 1, {})
                            unnested_rows[i] += 1

                        # unnest array into rows
                        for j, value in enumerate(values):
                            data[i + j][name] = value

                        # skip newly unnested rows
                        i += unnested_rows[i]

                    i += 1

            if column["type"] and column["type"].startswith("ROW("):
                # expand columns; we append them to the left so they are added
                # immediately after the parent
                expanded = get_children(column)
                to_process.extendleft((column, level) for column in expanded[::-1])
                expanded_columns.extend(expanded)

                # expand row objects into new columns
                for row in data:
                    values = row.get(name) or []
                    if isinstance(values, str):
                        values = cast(Optional[list[Any]], destringify(values))
                        row[name] = values
                    for value, col in zip(values or [], expanded, strict=False):
                        row[col["column_name"]] = value

        data = [
            {k["column_name"]: row.get(k["column_name"], "") for k in all_columns}
            for row in data
        ]

        return all_columns, data, expanded_columns

    @classmethod
    def get_extra_table_metadata(
        cls,
        database: Database,
        table: Table,
    ) -> dict[str, Any]:
        metadata = {}

        with database.get_inspector(
            catalog=table.catalog,
            schema=table.schema,
        ) as inspector:
            try:
                indexes = cls.get_indexes(database, inspector, table)
            except Exception:  # noqa: BLE001  # pylint: disable=broad-except
                indexes = []

        try:
            if indexes:
                col_names, latest_parts = cls.latest_partition(
                    database,
                    table,
                    show_first=True,
                    indexes=indexes,
                )

                if not latest_parts:
                    latest_parts = tuple([None] * len(col_names))

                metadata["partitions"] = {
                    "cols": sorted(indexes[0].get("column_names", [])),
                    "latest": dict(zip(col_names, latest_parts, strict=False)),
                    "partitionQuery": cls._partition_query(
                        table=table,
                        indexes=indexes,
                        database=database,
                    ),
                }
        except NoSuchTableError as ex:
            raise SupersetDBAPIProgrammingError(
                "Table doesn't seem to exist on the database"
            ) from ex

        metadata["view"] = cast(
            Any,
            cls.get_create_view(database, table.schema, table.table),
        )

        return metadata

    @classmethod
    def get_create_view(
        cls, database: Database, schema: str | None, table: str
    ) -> str | None:
        """
        Return a CREATE VIEW statement, or `None` if not a view.

        :param database: Database instance
        :param schema: Schema name
        :param table: Table (view) name
        """
        # pylint: disable=import-outside-toplevel
        from pyhive.exc import DatabaseError

        with _raw_connection(database, schema=schema) as conn:
            cursor = conn.cursor()
            sql = f"SHOW CREATE VIEW {schema}.{table}"
            try:
                cls.execute(cursor, sql, database)
                rows = cls.fetch_data(cursor, 1)

                return rows[0][0]
            except DatabaseError:  # not a VIEW
                return None

    @classmethod
    def get_tracking_url(cls, cursor: Cursor) -> str | None:
        with contextlib.suppress(AttributeError):
            if cursor.last_query_id:
                # pylint: disable=protected-access, line-too-long
                return f"{cursor._protocol}://{cursor._host}:{cursor._port}/ui/query.html?{cursor.last_query_id}"
        return None

    @classmethod
    def handle_cursor(cls, cursor: Cursor, query: Query) -> None:
        """Updates progress information"""
        from sqlalchemy.orm import object_session

        from superset.config import SupersetSettings

        if tracking_url := cls.get_tracking_url(cursor):
            query.tracking_url_raw = tracking_url
            session = object_session(query)
            if session is not None:
                session.commit()

        query_id = query.id
        poll_interval = query.database.connect_args.get(
            "poll_interval", SupersetSettings().presto_poll_interval
        )
        logger.info("Query %i: Polling the cursor for progress", query_id)
        polled = cursor.poll()
        # poll returns dict -- JSON status information or ``None``
        # if the query is done
        # https://github.com/dropbox/PyHive/blob/
        # b34bdbf51378b3979eaf5eca9e956f06ddc36ca0/pyhive/presto.py#L178
        while polled:
            # Update the object and wait for the kill signal.
            stats = polled.get("stats", {})

            session = object_session(query)
            if session is not None:
                query = session.query(type(query)).filter_by(id=query_id).one()
            if query.status in [QueryStatus.STOPPED, QueryStatus.TIMED_OUT]:
                cursor.cancel()
                break

            if stats:
                state = stats.get("state")

                # if already finished, then stop polling
                if state == "FINISHED":
                    break

                completed_splits = float(stats.get("completedSplits"))
                total_splits = float(stats.get("totalSplits"))
                if total_splits and completed_splits:
                    progress = 100 * (completed_splits / total_splits)
                    logger.info(
                        "Query %s progress: %s / %s splits",
                        query_id,
                        completed_splits,
                        total_splits,
                    )
                    query.progress = max(query.progress, progress)
                    if session is not None:
                        session.commit()
            time.sleep(poll_interval)
            logger.info("Query %i: Polling the cursor for progress", query_id)
            polled = cursor.poll()

    @classmethod
    def _extract_error_message(cls, ex: Exception) -> str:
        if (
            hasattr(ex, "orig")
            and type(ex.orig).__name__ == "DatabaseError"
            and isinstance(ex.orig[0], dict)
        ):
            error_dict = ex.orig[0]
            # pylint: disable=consider-using-f-string
            return "{} at {}: {}".format(
                error_dict.get("errorName"),
                error_dict.get("errorLocation"),
                error_dict.get("message"),
            )
        if type(ex).__name__ == "DatabaseError" and hasattr(ex, "args") and ex.args:
            error_dict = ex.args[0]
            return error_dict.get("message", "Unknown Presto Error")
        return utils.error_msg_from_exception(ex)

    @classmethod
    def has_implicit_cancel(cls) -> bool:
        """
        Return True if the live cursor handles the implicit cancelation of the query,
        False otherwise.

        :return: Whether the live cursor implicitly cancels the query
        :see: handle_cursor
        """

        return True


__all__ = [
    "PrestoEngineSpec",
]
