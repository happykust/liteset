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
"""Async QueryContext — top-level container for datasource + queries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from superset.common.query_object import AsyncQueryObject


@dataclass
class AsyncQueryContext:
    """Container binding a datasource with one or more query objects.

    Mirrors superset.common.query_context.QueryContext for API compat.
    Processed by AsyncQueryContextProcessor.get_payload().
    """

    datasource: Any  # BaseDatasource (from superset.models.connectors)
    queries: list[AsyncQueryObject] = field(default_factory=list)
    form_data: dict[str, Any] = field(default_factory=dict)
    force: bool = False
    custom_cache_timeout: int | None = None
    result_type: str | None = None
    result_format: str | None = None
    slice_: Any = None  # Slice model, for per-chart cache timeout
    cache_values: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Apply the granularity transform to each query object.

        1:1 with ``QueryContextFactory._process_query_object``
        (``superset_old/common/query_context_factory.py:115-123``): the factory
        runs ``_apply_granularity(qo, form_data, datasource)`` then
        ``_apply_filters(qo)`` while building each query object. ``_apply_filters``
        is self-contained and already runs in ``AsyncQueryObject.__post_init__``;
        ``_apply_granularity`` additionally needs the request ``form_data`` (for
        ``x_axis``) and the resolved datasource (for its temporal columns), which
        are only known here — so this is the equivalent build hook.

        Guarded so a missing datasource / column metadata (e.g. a SQL Lab
        ``Query`` datasource that has no ``columns``) never breaks context
        construction.
        """
        if self.datasource is None:
            return
        for query_object in self.queries:
            try:
                query_object.apply_granularity(self.form_data, self.datasource)
            except Exception:  # noqa: BLE001, S112
                # Best-effort, matching the factory's tolerance of datasources
                # without standardized column metadata.
                continue
