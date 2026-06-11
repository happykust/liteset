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
"""Import/export controller for full asset bundles.

1:1 port of ``superset_old/importexport/api.py`` (``ImportExportRestApi``).

The original FAB ``method_permission_name`` map
(``superset_old/views/base_api.py:258-280``) renames ``export`` ->
``mulexport`` and ``import_`` -> ``add``, and FAB derives the resource name
from the class name ``ImportExportRestApi`` (the original sets no
``class_permission_name`` override).  The permission tuples below restore
those exact names so existing roles (``can_mulexport on ImportExportRestApi``
/ ``can_add on ImportExportRestApi``) keep passing the guard.
"""

from __future__ import annotations

import io
import json
from typing import Annotated, Any
from zipfile import is_zipfile, ZipFile

from litestar import Controller, get, post
from litestar.datastructures import UploadFile
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Response

from superset.commands.importers.exceptions import (
    IncorrectFormatError,
    NoValidFilesFoundError,
)
from superset.events import event_logger
from superset.guards.rbac import require_permission
from superset.typing import UserProtocol


class ImportExportController(Controller):
    path = "/api/v1/assets"
    tags = ["Import/Export"]

    @get(
        "/export/",
        guards=[require_permission("can_mulexport", "ImportExportRestApi")],
    )
    async def export_assets(
        self,
        session: Any,
        current_user: UserProtocol,
    ) -> Response[bytes]:
        """GET /api/v1/assets/export/ -- export all assets as ZIP."""
        from datetime import datetime

        from superset.importexport.manager import AsyncFullAssetManager

        # ONE timestamp for both the internal ZIP root and the download
        # filename — 1:1 with the original which assigns it once (an export
        # spanning a second boundary previously produced mismatched names).
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        root = f"assets_export_{timestamp}"

        manager = AsyncFullAssetManager(session)
        content = await manager.export_assets(root=root)

        await event_logger.alog_with_context("assets.export", user_id=current_user.id)

        filename = f"{root}.zip"

        return Response(
            content=content,
            media_type="application/zip",
            headers={
                "Content-Disposition": f"attachment; filename={filename}",
            },
        )

    @post(
        "/import/",
        guards=[require_permission("can_add", "ImportExportRestApi")],
    )
    async def import_assets(
        self,
        data: Annotated[
            dict[str, Any], Body(media_type=RequestEncodingType.MULTI_PART)
        ],
        session: Any = None,
        current_user: UserProtocol = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """POST /api/v1/assets/import/ -- import assets from ZIP.

        Mirrors ``ImportExportRestApi.import_``: the upload field is named
        ``bundle`` (``file`` accepted as an alias for clients that switched
        to the renamed field), an empty upload returns 400, a non-ZIP upload
        raises :class:`IncorrectFormatError` (422), an empty/invalid bundle
        raises :class:`NoValidFilesFoundError` (400), and schema/import
        failures bubble up as :class:`CommandInvalidError` (422).  On success
        the original returns ``{"message": "OK"}`` with HTTP 200.
        """
        from superset.importexport.manager import AsyncFullAssetManager

        # Original posts the field as ``bundle``; ``file`` kept as an alias.
        file: UploadFile | None = data.get("bundle") or data.get("file")
        # ``request.files.get("bundle")`` falsy -> ``response_400()``.
        if file is None:
            # Original ``response_400()`` -> HTTP 400.
            from superset.exceptions import CommandException

            exc = CommandException("Request is incorrect: bundle is required")
            exc.status_code = 400
            raise exc

        content = await file.read()

        # ``if not is_zipfile(upload): raise IncorrectFormatError("Not a ZIP file")``
        if not is_zipfile(io.BytesIO(content)):
            raise IncorrectFormatError("Not a ZIP file")

        # ``contents = get_contents_from_bundle(bundle)`` strips the root
        # folder via ``remove_root`` and keeps only valid YAML entries.
        from superset.commands.importers.v1.utils import get_contents_from_bundle

        with ZipFile(io.BytesIO(content)) as bundle:
            contents = get_contents_from_bundle(bundle)

        # ``if not contents: raise NoValidFilesFoundError()``
        if not contents:
            raise NoValidFilesFoundError()

        # ``sparse = request.form.get("sparse") == "true"`` — strict string
        # comparison (multipart values arrive as strings).
        sparse = data.get("sparse") == "true"

        # ``json.loads(request.form[...]) if ... in request.form else None``
        def _parse_json(raw: str | dict[str, Any] | None) -> dict[str, Any] | None:
            if raw is None:
                return None
            return json.loads(raw) if isinstance(raw, str) else raw

        passwords = _parse_json(data.get("passwords"))
        ssh_tunnel_passwords = _parse_json(data.get("ssh_tunnel_passwords"))
        ssh_tunnel_private_keys = _parse_json(data.get("ssh_tunnel_private_keys"))
        ssh_tunnel_private_key_passwords = _parse_json(
            data.get("ssh_tunnel_private_key_passwords")
        )

        manager = AsyncFullAssetManager(session)
        # Any schema/import failure raises (CommandInvalidError/IncorrectFormat/
        # NoValidFilesFound) and is surfaced as the matching 4xx by the global
        # exception handler — the manager no longer swallows them into a 200.
        await manager.import_assets(
            contents=contents,
            overwrite=bool(data.get("overwrite", False)),
            passwords=passwords,
            ssh_tunnel_passwords=ssh_tunnel_passwords,
            ssh_tunnel_private_keys=ssh_tunnel_private_keys,
            ssh_tunnel_private_key_passwords=ssh_tunnel_private_key_passwords,
            sparse=sparse,
            current_user=current_user,
        )

        await event_logger.alog_with_context(
            "assets.import",
            user_id=current_user.id if current_user else None,
        )

        # Original returns ``self.response(200, message="OK")``.
        return {"message": "OK"}
