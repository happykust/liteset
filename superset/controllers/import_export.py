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
"""Import/export controller for full asset bundles."""

from __future__ import annotations

import json
from typing import Annotated, Any

from litestar import Controller, get, post
from litestar.datastructures import UploadFile
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Response

from superset.events import event_logger
from superset.guards.rbac import require_permission
from superset.typing import UserProtocol


class ImportExportController(Controller):
    path = "/api/v1/assets"
    tags = ["Import/Export"]

    @get(
        "/export/",
        guards=[require_permission("can_read", "ImportExport")],
    )
    async def export_assets(
        self,
        session: Any,
        current_user: UserProtocol,
    ) -> Response[bytes]:
        """GET /api/v1/assets/export/ -- export all assets as ZIP."""
        from superset.importexport.manager import AsyncFullAssetManager

        manager = AsyncFullAssetManager(session)
        content = await manager.export_assets()

        await event_logger.alog_with_context("assets.export", user_id=current_user.id)

        return Response(
            content=content,
            media_type="application/zip",
            headers={
                "Content-Disposition": "attachment; filename=assets_export.zip",
            },
        )

    @post(
        "/import/",
        guards=[require_permission("can_write", "ImportExport")],
    )
    async def import_assets(
        self,
        data: Annotated[
            dict[str, Any], Body(media_type=RequestEncodingType.MULTI_PART)
        ],
        session: Any = None,
        current_user: UserProtocol = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """POST /api/v1/assets/import/ -- import assets from ZIP."""
        from superset.importexport.manager import AsyncFullAssetManager

        file: UploadFile | None = data.get("file")
        overwrite = data.get("overwrite", False)
        sparse = data.get("sparse", False)
        passwords_raw = data.get("passwords", "{}")
        ssh_tunnel_passwords_raw = data.get("ssh_tunnel_passwords", "{}")
        ssh_tunnel_private_keys_raw = data.get("ssh_tunnel_private_keys", "{}")
        ssh_tunnel_private_key_passwords_raw = data.get(
            "ssh_tunnel_private_key_passwords", "{}"
        )

        if file is None:
            from superset.exceptions import CommandInvalidError

            raise CommandInvalidError("file is required")

        def _parse_json(raw: str | dict[str, Any] | None) -> dict[str, Any]:
            if raw is None:
                return {}
            return json.loads(raw) if isinstance(raw, str) else raw

        passwords = _parse_json(passwords_raw)
        ssh_tunnel_passwords = _parse_json(ssh_tunnel_passwords_raw)
        ssh_tunnel_private_keys = _parse_json(ssh_tunnel_private_keys_raw)
        ssh_tunnel_private_key_passwords = _parse_json(
            ssh_tunnel_private_key_passwords_raw
        )

        content = await file.read()
        manager = AsyncFullAssetManager(session)
        result = await manager.import_assets(
            file_content=content,
            overwrite=bool(overwrite),
            passwords=passwords,
            ssh_tunnel_passwords=ssh_tunnel_passwords,
            ssh_tunnel_private_keys=ssh_tunnel_private_keys,
            ssh_tunnel_private_key_passwords=ssh_tunnel_private_key_passwords,
            sparse=bool(sparse),
        )

        await event_logger.alog_with_context(
            "assets.import",
            user_id=current_user.id if current_user else None,
        )

        if not result.success:
            return {"message": "Import completed with errors", "errors": result.errors}

        return {"message": "Import successful", "imported": result.imported}
