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
"""Flask WSGI fallback for Strangler Fig migration."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from litestar import asgi, Litestar

if TYPE_CHECKING:
    from litestar.types import Receive, Scope, Send

logger = logging.getLogger(__name__)


async def init_flask_fallback(app: Litestar) -> None:
    """Initialize Flask ASGI wrapper during on_startup.

    Eager init avoids race conditions from lazy global init.
    """
    from asgiref.wsgi import WsgiToAsgi

    from superset.app import create_app as create_flask_app

    logger.info("Initializing Flask fallback for non-migrated routes")
    flask_app = create_flask_app()
    app.state.flask_asgi = WsgiToAsgi(flask_app)


def create_flask_fallback() -> Any:
    """Create ASGI mount that delegates to Flask via app.state."""

    @asgi("/", is_mount=True, copy_scope=True)
    async def flask_fallback(scope: "Scope", receive: "Receive", send: "Send") -> None:
        flask_asgi = scope["app"].state.flask_asgi
        await flask_asgi(scope, receive, send)

    return flask_fallback
