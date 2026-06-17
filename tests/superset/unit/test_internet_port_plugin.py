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
"""Unit tests for the ``internet_port`` advanced data type plugin.

Guards ``port_translation_func`` against a regression where
``valid_filter_operators`` was incorrectly populated with display strings
instead of the :class:`FilterStringOperators` enum members the
frontend/contract expects.
"""

from superset.advanced_data_type.plugins.internet_port import (
    internet_port as port,
    port_translation_func,
)
from superset.advanced_data_type.types import (
    AdvancedDataTypeRequest,
    AdvancedDataTypeResponse,
)
from superset.utils.core import FilterStringOperators

EXPECTED_OPERATORS = [
    FilterStringOperators.EQUALS,
    FilterStringOperators.GREATER_THAN_OR_EQUAL,
    FilterStringOperators.GREATER_THAN,
    FilterStringOperators.IN,
    FilterStringOperators.LESS_THAN,
    FilterStringOperators.LESS_THAN_OR_EQUAL,
]


def test_port_translation_func_valid_filter_operators_are_enum_members() -> None:
    """``port_translation_func`` must set ``valid_filter_operators`` to the
    ``FilterStringOperators`` enum members (matching upstream), not display
    strings."""
    req: AdvancedDataTypeRequest = {
        "advanced_data_type": "port",
        "values": ["80"],
    }
    resp: AdvancedDataTypeResponse = port_translation_func(req)
    assert resp["valid_filter_operators"] == EXPECTED_OPERATORS


def test_port_translation_func_valid_port_number() -> None:
    req: AdvancedDataTypeRequest = {
        "advanced_data_type": "port",
        "values": ["80"],
    }
    expected: AdvancedDataTypeResponse = {
        "values": [[80]],
        "error_message": "",
        "display_value": "[80]",
        "valid_filter_operators": EXPECTED_OPERATORS,
    }
    assert port.translate_type(req) == expected


def test_port_translation_func_valid_port_name() -> None:
    req: AdvancedDataTypeRequest = {
        "advanced_data_type": "port",
        "values": ["https"],
    }
    expected: AdvancedDataTypeResponse = {
        "values": [[443]],
        "error_message": "",
        "display_value": "[443]",
        "valid_filter_operators": EXPECTED_OPERATORS,
    }
    assert port.translate_type(req) == expected
