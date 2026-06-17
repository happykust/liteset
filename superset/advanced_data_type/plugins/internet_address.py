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
"""``internet_address`` advanced data type plugin.

Represents both an IP address and a CIDR range, parsing the user's
input via :mod:`ipaddress` and translating filter expressions on the
underlying integer-typed column into SQLAlchemy clauses (single value
or ``[start, end]`` range comparisons).
"""

from __future__ import annotations

import ipaddress
from typing import Any

from sqlalchemy import Column

from superset.advanced_data_type.types import (
    AdvancedDataType,
    AdvancedDataTypeRequest,
    AdvancedDataTypeResponse,
)
from superset.utils.core import FilterOperator, FilterStringOperators


def cidr_func(req: AdvancedDataTypeRequest) -> AdvancedDataTypeResponse:
    """Convert a passed-in :class:`AdvancedDataTypeRequest` to a response.

    Each input value is parsed as either a numeric IP literal or a
    standard ``a.b.c.d[/n]`` / ``::/n`` CIDR string.  Single hosts are
    surfaced as the integer value, ranges as ``{"start": int, "end": int}``.
    """
    resp: AdvancedDataTypeResponse = {
        "values": [],
        "error_message": "",
        "display_value": "",
        "valid_filter_operators": [
            FilterStringOperators.EQUALS,
            FilterStringOperators.GREATER_THAN_OR_EQUAL,
            FilterStringOperators.GREATER_THAN,
            FilterStringOperators.IN,
            FilterStringOperators.LESS_THAN,
            FilterStringOperators.LESS_THAN_OR_EQUAL,
        ],
    }
    if req["values"] == [""]:
        resp["values"].append("")
        return resp
    for val in req["values"]:
        string_value = str(val)
        try:
            ip_range = (
                ipaddress.ip_network(int(string_value), strict=False)
                if string_value.isnumeric()
                else ipaddress.ip_network(string_value, strict=False)
            )
            resp["values"].append(
                {"start": int(ip_range[0]), "end": int(ip_range[-1])}
                if ip_range[0] != ip_range[-1]
                else int(ip_range[0])
            )
        except ValueError as ex:
            resp["error_message"] = str(ex)
            break
        else:
            resp["display_value"] = ", ".join(
                map(  # noqa: C417
                    lambda x: (
                        f"{x['start']} - {x['end']}" if isinstance(x, dict) else str(x)
                    ),
                    resp["values"],
                )
            )
    return resp


def cidr_translate_filter_func(  # noqa: C901
    col: Column[Any], operator: FilterOperator, values: list[Any]
) -> Any:
    """Build a SQLAlchemy expression for a CIDR-aware filter.

    Accepts a ``Column``, a :class:`FilterOperator` and a list of values
    (which may be ``int`` for single hosts or ``{"start", "end"}`` dicts
    for ranges) and returns the corresponding SQL boolean expression.
    """
    return_expression: Any
    if operator in (FilterOperator.IN, FilterOperator.NOT_IN):
        dict_items = [val for val in values if isinstance(val, dict)]
        single_values = [val for val in values if not isinstance(val, dict)]
        if operator == FilterOperator.IN:
            cond = col.in_(single_values)
            for dictionary in dict_items:
                cond = cond | (col <= dictionary["end"]) & (col >= dictionary["start"])
        elif operator == FilterOperator.NOT_IN:
            cond = ~(col.in_(single_values))
            for dictionary in dict_items:
                cond = cond & (col > dictionary["end"]) & (col < dictionary["start"])
        return_expression = cond
    if len(values) == 1:
        value = values[0]
        if operator == FilterOperator.EQUALS:
            return_expression = (
                col == value
                if not isinstance(value, dict)
                else (col <= value["end"]) & (col >= value["start"])
            )
        if operator == FilterOperator.GREATER_THAN_OR_EQUALS:
            return_expression = (
                col >= value if not isinstance(value, dict) else col >= value["end"]
            )
        if operator == FilterOperator.GREATER_THAN:
            return_expression = (
                col > value if not isinstance(value, dict) else col > value["end"]
            )
        if operator == FilterOperator.LESS_THAN:
            return_expression = (
                col < value if not isinstance(value, dict) else col < value["start"]
            )
        if operator == FilterOperator.LESS_THAN_OR_EQUALS:
            return_expression = (
                col <= value if not isinstance(value, dict) else col <= value["start"]
            )
        if operator == FilterOperator.NOT_EQUALS:
            return_expression = (
                col != value
                if not isinstance(value, dict)
                else (col > value["end"]) | (col < value["start"])
            )
    return return_expression


internet_address: AdvancedDataType = AdvancedDataType(
    verbose_name="internet address",
    description="represents both an ip and cidr range",
    valid_data_types=["int"],
    translate_filter=cidr_translate_filter_func,
    translate_type=cidr_func,
)
