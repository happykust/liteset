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

The chart controller inlines ``build_job_metadata`` + ``maybe_forward_guest_token``
directly, so no separate command class is needed here.

``AsyncQueryTokenException`` is the public API raised by the app-startup JWT-secret
guard (``superset.app._validate_global_async_queries_config``).
"""

from __future__ import annotations


class AsyncQueryTokenException(Exception):  # noqa: N818
    """Raised when the JWT channel-token cookie is missing or invalid."""
