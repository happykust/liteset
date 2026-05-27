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
"""Async-query token exception.

Historically this module also held a ``CreateAsyncChartDataJobCommand``
(an async port of
``superset_old/commands/chart/data/create_async_job_command.py``).  That
command was never instantiated: the chart controller inlines
``build_job_metadata`` + ``maybe_forward_guest_token`` directly, and the
command's ``run()`` omitted ``maybe_forward_guest_token`` — which would have
broken embedded-guest RLS had it ever been wired up.  It has therefore been
removed to avoid the dead, divergent code path.

``AsyncQueryTokenException`` is retained: it is the live, public API the
app-build-time JWT-secret guard (``superset.app._validate_global_async_queries_config``)
raises, 1:1 with the original ``AsyncQueryManager.init_app`` guard.
"""

from __future__ import annotations


class AsyncQueryTokenException(Exception):  # noqa: N818  # 1:1 with original public API
    """Raised when the JWT channel-token cookie is missing or invalid.

    1:1 with
    ``superset_old.async_events.async_query_manager.AsyncQueryTokenException``.
    """
