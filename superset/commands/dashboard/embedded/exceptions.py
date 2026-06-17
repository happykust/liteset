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
"""Exceptions for embedded dashboard commands."""

from __future__ import annotations

from typing import Optional

from superset.exceptions import ForbiddenError, ObjectNotFoundError
from superset.i18n import gettext as _


class EmbeddedDashboardNotFoundError(ObjectNotFoundError):
    def __init__(
        self,
        embedded_dashboard_uuid: Optional[str] = None,
        exception: Optional[Exception] = None,  # noqa: ARG002
    ) -> None:
        # Legacy signature included an ``exception`` arg used only as __cause__
        # in raise-chains. Accept it for compatibility but ignore it here.
        super().__init__("EmbeddedDashboard", embedded_dashboard_uuid)


class EmbeddedDashboardAccessDeniedError(ForbiddenError):
    message = _("You don't have access to this embedded dashboard config.")
