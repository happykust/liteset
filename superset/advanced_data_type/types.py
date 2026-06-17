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
"""Public types for advanced data type plugins.

These types are part of the plugin contract, so any third-party plugin
imported via the ``ADVANCED_DATA_TYPES`` config will keep working.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, TypedDict


class AdvancedDataTypeRequest(TypedDict):
    """Request shape passed to ``translate_type``."""

    advanced_data_type: str
    values: list[Any]


class AdvancedDataTypeResponse(TypedDict, total=False):
    """Response shape returned from ``translate_type``."""

    error_message: str | None
    values: list[Any]
    display_value: str
    valid_filter_operators: list[str]


@dataclass
class AdvancedDataType:
    """Used for converting base type value into an advanced type value."""

    verbose_name: str
    description: str
    valid_data_types: list[str]
    translate_type: Callable[[AdvancedDataTypeRequest], AdvancedDataTypeResponse]
    translate_filter: Callable[..., Any]
