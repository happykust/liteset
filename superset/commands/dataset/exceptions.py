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

from superset.exceptions import (
    CommandException,
    CommandInvalidError,
    ObjectNotFoundError,
)
from superset.i18n import gettext as _


class WarmUpCacheTableNotFoundError(CommandException):
    # ``status`` mirrors ``superset_old/commands/dataset/exceptions.py:205``
    # (1:1 with original).  ``status_code`` is kept in sync so the Liteset
    # exception-to-HTTP mapping (which keys off ``status_code`` everywhere
    # else) still emits the correct 404.
    status = 404
    status_code = 404
    message = _("The provided table was not found in the provided database")


__all__ = (
    "CommandInvalidError",
    "ObjectNotFoundError",
    "WarmUpCacheTableNotFoundError",
)
