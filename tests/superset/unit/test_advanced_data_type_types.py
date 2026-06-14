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
"""Pure-function unit tests for the advanced data type plugins.

Ported 1:1 from the vendored upstream
``tests/unit_tests/advanced_data_type/types_tests.py``. These exercise the
``translate_type`` / ``translate_filter`` callables on the ``internet_address``
(cidr) and ``internet_port`` plugins. No Flask / FAB involved.
"""

import sqlalchemy
from sqlalchemy import Column, Integer

from superset.advanced_data_type.plugins.internet_address import internet_address
from superset.advanced_data_type.plugins.internet_port import internet_port as port
from superset.advanced_data_type.types import (
    AdvancedDataTypeRequest,
    AdvancedDataTypeResponse,
)
from superset.utils.core import FilterOperator, FilterStringOperators

EXPECTED_OPERATORS = [
    FilterStringOperators.EQUALS,
    FilterStringOperators.GREATER_THAN_OR_EQUAL,
    FilterStringOperators.GREATER_THAN,
    FilterStringOperators.IN,
    FilterStringOperators.LESS_THAN,
    FilterStringOperators.LESS_THAN_OR_EQUAL,
]


def test_ip_func_valid_ip() -> None:
    """``cidr_func`` behaves as expected when a valid IP is passed in."""
    cidr_request: AdvancedDataTypeRequest = {
        "advanced_data_type": "cidr",
        "values": ["1.1.1.1"],
    }
    cidr_response: AdvancedDataTypeResponse = {
        "values": [16843009],
        "error_message": "",
        "display_value": "16843009",
        "valid_filter_operators": EXPECTED_OPERATORS,
    }

    assert internet_address.translate_type(cidr_request) == cidr_response


def test_cidr_func_invalid_ip() -> None:
    """``cidr_func`` behaves as expected when an invalid IP is passed in."""
    cidr_request: AdvancedDataTypeRequest = {
        "advanced_data_type": "cidr",
        "values": ["abc"],
    }
    cidr_response: AdvancedDataTypeResponse = {
        "values": [],
        "error_message": "'abc' does not appear to be an IPv4 or IPv6 network",
        "display_value": "",
        "valid_filter_operators": EXPECTED_OPERATORS,
    }

    assert internet_address.translate_type(cidr_request) == cidr_response


def test_cidr_func_empty_ip() -> None:
    """``cidr_func`` behaves as expected when no IP is passed in."""
    cidr_request: AdvancedDataTypeRequest = {
        "advanced_data_type": "cidr",
        "values": [""],
    }
    cidr_response: AdvancedDataTypeResponse = {
        "values": [""],
        "error_message": "",
        "display_value": "",
        "valid_filter_operators": EXPECTED_OPERATORS,
    }

    assert internet_address.translate_type(cidr_request) == cidr_response


def test_port_translation_func_valid_port_number() -> None:
    """``port_translation_func`` behaves as expected for a valid port number."""
    port_request: AdvancedDataTypeRequest = {
        "advanced_data_type": "port",
        "values": ["80"],
    }
    port_response: AdvancedDataTypeResponse = {
        "values": [[80]],
        "error_message": "",
        "display_value": "[80]",
        "valid_filter_operators": EXPECTED_OPERATORS,
    }

    assert port.translate_type(port_request) == port_response


def test_port_translation_func_valid_port_name() -> None:
    """``port_translation_func`` behaves as expected for a valid port name."""
    port_request: AdvancedDataTypeRequest = {
        "advanced_data_type": "port",
        "values": ["https"],
    }
    port_response: AdvancedDataTypeResponse = {
        "values": [[443]],
        "error_message": "",
        "display_value": "[443]",
        "valid_filter_operators": EXPECTED_OPERATORS,
    }

    assert port.translate_type(port_request) == port_response


def test_port_translation_func_invalid_port_name() -> None:
    """``port_translation_func`` behaves as expected for an invalid port name."""
    port_request: AdvancedDataTypeRequest = {
        "advanced_data_type": "port",
        "values": ["abc"],
    }
    port_response: AdvancedDataTypeResponse = {
        "values": [],
        "error_message": "'abc' does not appear to be a port name or number",
        "display_value": "",
        "valid_filter_operators": EXPECTED_OPERATORS,
    }

    assert port.translate_type(port_request) == port_response


def test_port_translation_func_invalid_port_number() -> None:
    """``port_translation_func`` behaves as expected for an invalid port number."""
    port_request: AdvancedDataTypeRequest = {
        "advanced_data_type": "port",
        "values": ["123456789"],
    }
    port_response: AdvancedDataTypeResponse = {
        "values": [],
        "error_message": "'123456789' does not appear to be a port name or number",
        "display_value": "",
        "valid_filter_operators": EXPECTED_OPERATORS,
    }

    assert port.translate_type(port_request) == port_response


def test_port_translation_func_empty_port_number() -> None:
    """``port_translation_func`` behaves as expected when no port is passed in."""
    port_request: AdvancedDataTypeRequest = {
        "advanced_data_type": "port",
        "values": [""],
    }
    port_response: AdvancedDataTypeResponse = {
        "values": [[""]],
        "error_message": "",
        "display_value": "",
        "valid_filter_operators": EXPECTED_OPERATORS,
    }

    assert port.translate_type(port_request) == port_response


def test_cidr_translate_filter_func_equals() -> None:
    """``cidr_translate_filter_func`` with the EQUALS operator."""
    input_column = Column("user_ip", Integer)
    input_operation = FilterOperator.EQUALS
    input_values = [16843009]

    cidr_translate_filter_response = input_column == input_values[0]

    assert internet_address.translate_filter(
        input_column, input_operation, input_values
    ).compare(cidr_translate_filter_response)


def test_cidr_translate_filter_func_not_equals() -> None:
    """``cidr_translate_filter_func`` with the NOT_EQUALS operator."""
    input_column = Column("user_ip", Integer)
    input_operation = FilterOperator.NOT_EQUALS
    input_values = [16843009]

    cidr_translate_filter_response = input_column != input_values[0]

    assert internet_address.translate_filter(
        input_column, input_operation, input_values
    ).compare(cidr_translate_filter_response)


def test_cidr_translate_filter_func_greater_than_or_equals() -> None:
    """``cidr_translate_filter_func`` with the GREATER_THAN_OR_EQUALS operator."""
    input_column = Column("user_ip", Integer)
    input_operation = FilterOperator.GREATER_THAN_OR_EQUALS
    input_values = [16843009]

    cidr_translate_filter_response: sqlalchemy.sql.expression.BinaryExpression = (
        input_column >= input_values[0]
    )

    assert internet_address.translate_filter(
        input_column, input_operation, input_values
    ).compare(cidr_translate_filter_response)


def test_cidr_translate_filter_func_greater_than() -> None:
    """``cidr_translate_filter_func`` with the GREATER_THAN operator."""
    input_column = Column("user_ip", Integer)
    input_operation = FilterOperator.GREATER_THAN
    input_values = [16843009]

    cidr_translate_filter_response: sqlalchemy.sql.expression.BinaryExpression = (
        input_column > input_values[0]
    )

    assert internet_address.translate_filter(
        input_column, input_operation, input_values
    ).compare(cidr_translate_filter_response)


def test_cidr_translate_filter_func_less_than() -> None:
    """``cidr_translate_filter_func`` with the LESS_THAN operator."""
    input_column = Column("user_ip", Integer)
    input_operation = FilterOperator.LESS_THAN
    input_values = [16843009]

    cidr_translate_filter_response: sqlalchemy.sql.expression.BinaryExpression = (
        input_column < input_values[0]
    )

    assert internet_address.translate_filter(
        input_column, input_operation, input_values
    ).compare(cidr_translate_filter_response)


def test_cidr_translate_filter_func_less_than_or_equals() -> None:
    """``cidr_translate_filter_func`` with the LESS_THAN_OR_EQUALS operator."""
    input_column = Column("user_ip", Integer)
    input_operation = FilterOperator.LESS_THAN_OR_EQUALS
    input_values = [16843009]

    cidr_translate_filter_response: sqlalchemy.sql.expression.BinaryExpression = (
        input_column <= input_values[0]
    )

    assert internet_address.translate_filter(
        input_column, input_operation, input_values
    ).compare(cidr_translate_filter_response)


def test_cidr_translate_filter_func_in_single() -> None:
    """``cidr_translate_filter_func`` with the IN operator and a single IP."""
    input_column = Column("user_ip", Integer)
    input_operation = FilterOperator.IN
    input_values = [16843009]

    cidr_translate_filter_response: sqlalchemy.sql.expression.BinaryExpression = (
        input_column.in_(input_values)
    )

    assert internet_address.translate_filter(
        input_column, input_operation, input_values
    ).compare(cidr_translate_filter_response)


def test_cidr_translate_filter_func_in_double() -> None:
    """``cidr_translate_filter_func`` with the IN operator and two IPs."""
    input_column = Column("user_ip", Integer)
    input_operation = FilterOperator.IN
    input_values = [{"start": 16843009, "end": 33686018}]

    input_condition = input_column.in_([])

    cidr_translate_filter_response: sqlalchemy.sql.expression.BinaryExpression = (
        input_condition | ((input_column <= 33686018) & (input_column >= 16843009))
    )

    assert internet_address.translate_filter(
        input_column, input_operation, input_values
    ).compare(cidr_translate_filter_response)


def test_cidr_translate_filter_func_not_in_single() -> None:
    """``cidr_translate_filter_func`` with the NOT_IN operator and a single IP."""
    input_column = Column("user_ip", Integer)
    input_operation = FilterOperator.NOT_IN
    input_values = [16843009]

    cidr_translate_filter_response: sqlalchemy.sql.expression.BinaryExpression = ~(
        input_column.in_(input_values)
    )

    assert internet_address.translate_filter(
        input_column, input_operation, input_values
    ).compare(cidr_translate_filter_response)


def test_cidr_translate_filter_func_not_in_double() -> None:
    """``cidr_translate_filter_func`` with the NOT_IN operator and two IPs."""
    input_column = Column("user_ip", Integer)
    input_operation = FilterOperator.NOT_IN
    input_values = [{"start": 16843009, "end": 33686018}]

    input_condition = ~(input_column.in_([]))

    cidr_translate_filter_response: sqlalchemy.sql.expression.BinaryExpression = (
        input_condition & (input_column > 33686018) & (input_column < 16843009)
    )

    assert internet_address.translate_filter(
        input_column, input_operation, input_values
    ).compare(cidr_translate_filter_response)


def test_port_translate_filter_func_equals() -> None:
    """``port_translate_filter_func`` with the EQUALS operator."""
    input_column = Column("user_ip", Integer)
    input_operation = FilterOperator.EQUALS
    input_values = [[443]]

    port_translate_filter_response: sqlalchemy.sql.expression.BinaryExpression = (
        input_column.in_(input_values[0])
    )

    assert port.translate_filter(input_column, input_operation, input_values).compare(
        port_translate_filter_response
    )


def test_port_translate_filter_func_not_equals() -> None:
    """``port_translate_filter_func`` with the NOT_EQUALS operator."""
    input_column = Column("user_ip", Integer)
    input_operation = FilterOperator.NOT_EQUALS
    input_values = [[443]]

    port_translate_filter_response: sqlalchemy.sql.expression.BinaryExpression = ~(
        input_column.in_(input_values[0])
    )

    assert port.translate_filter(input_column, input_operation, input_values).compare(
        port_translate_filter_response
    )


def test_port_translate_filter_func_greater_than_or_equals() -> None:
    """``port_translate_filter_func`` with the GREATER_THAN_OR_EQUALS operator."""
    input_column = Column("user_ip", Integer)
    input_operation = FilterOperator.GREATER_THAN_OR_EQUALS
    input_values = [[443]]

    port_translate_filter_response: sqlalchemy.sql.expression.BinaryExpression = (
        input_column >= input_values[0][0]
    )

    assert port.translate_filter(input_column, input_operation, input_values).compare(
        port_translate_filter_response
    )


def test_port_translate_filter_func_greater_than() -> None:
    """``port_translate_filter_func`` with the GREATER_THAN operator."""
    input_column = Column("user_ip", Integer)
    input_operation = FilterOperator.GREATER_THAN
    input_values = [[443]]

    port_translate_filter_response: sqlalchemy.sql.expression.BinaryExpression = (
        input_column > input_values[0][0]
    )

    assert port.translate_filter(input_column, input_operation, input_values).compare(
        port_translate_filter_response
    )


def test_port_translate_filter_func_less_than_or_equals() -> None:
    """``port_translate_filter_func`` with the LESS_THAN_OR_EQUALS operator."""
    input_column = Column("user_ip", Integer)
    input_operation = FilterOperator.LESS_THAN_OR_EQUALS
    input_values = [[443]]

    port_translate_filter_response: sqlalchemy.sql.expression.BinaryExpression = (
        input_column <= input_values[0][0]
    )

    assert port.translate_filter(input_column, input_operation, input_values).compare(
        port_translate_filter_response
    )


def test_port_translate_filter_func_less_than() -> None:
    """``port_translate_filter_func`` with the LESS_THAN operator."""
    input_column = Column("user_ip", Integer)
    input_operation = FilterOperator.LESS_THAN
    input_values = [[443]]

    port_translate_filter_response: sqlalchemy.sql.expression.BinaryExpression = (
        input_column < input_values[0][0]
    )

    assert port.translate_filter(input_column, input_operation, input_values).compare(
        port_translate_filter_response
    )


def test_port_translate_filter_func_in_single() -> None:
    """``port_translate_filter_func`` with the IN operator and a single port."""
    input_column = Column("user_ip", Integer)
    input_operation = FilterOperator.IN
    input_values = [[443]]

    port_translate_filter_response: sqlalchemy.sql.expression.BinaryExpression = (
        input_column.in_(input_values[0])
    )

    assert port.translate_filter(input_column, input_operation, input_values).compare(
        port_translate_filter_response
    )


def test_port_translate_filter_func_in_double() -> None:
    """``port_translate_filter_func`` with the IN operator and two ports."""
    input_column = Column("user_ip", Integer)
    input_operation = FilterOperator.IN
    input_values = [[443, 80]]

    port_translate_filter_response: sqlalchemy.sql.expression.BinaryExpression = (
        input_column.in_(input_values[0])
    )

    assert port.translate_filter(input_column, input_operation, input_values).compare(
        port_translate_filter_response
    )


def test_port_translate_filter_func_not_in_single() -> None:
    """``port_translate_filter_func`` with the NOT_IN operator and a single port."""
    input_column = Column("user_ip", Integer)
    input_operation = FilterOperator.NOT_IN
    input_values = [[443]]

    port_translate_filter_response: sqlalchemy.sql.expression.BinaryExpression = ~(
        input_column.in_(input_values[0])
    )

    assert port.translate_filter(input_column, input_operation, input_values).compare(
        port_translate_filter_response
    )


def test_port_translate_filter_func_not_in_double() -> None:
    """``port_translate_filter_func`` with the NOT_IN operator and two ports."""
    input_column = Column("user_ip", Integer)
    input_operation = FilterOperator.NOT_IN
    input_values = [[443, 80]]

    port_translate_filter_response: sqlalchemy.sql.expression.BinaryExpression = ~(
        input_column.in_(input_values[0])
    )

    assert port.translate_filter(input_column, input_operation, input_values).compare(
        port_translate_filter_response
    )
