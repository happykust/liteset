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
"""CLI command that regenerates ``docs/static/resources/openapi.json``.

Liteset has no FAB; instead, the canonical OpenAPI schema is the one
Litestar synthesises from every registered controller, which is reachable
at runtime via the standard ``/swagger/v1`` endpoint.

This command boots the Litestar app in-process, asks Litestar for the
generated OpenAPI document, and serialises it to disk under
``docs/static/resources/openapi.json``.
"""

from __future__ import annotations

import json
import os
import sys

import click


@click.command("update-api-docs")
def update_api_docs() -> None:
    """Regenerate the ``openapi.json`` file in ``docs/static/resources``.

    Writes to ``docs/static/resources/openapi.json`` using the ``v1`` API
    version filter; exits non-zero on failure.
    """
    from superset.app import create_app

    app = create_app()

    schema = app.openapi_schema
    if schema is None:
        click.secho("API version not found", err=True)
        sys.exit(1)

    if hasattr(schema, "to_schema"):
        # Litestar 2.x ``OpenAPI.to_schema`` returns a vanilla dict.
        document = schema.to_schema()
    else:
        document = json.loads(json.dumps(schema, default=str))

    superset_dir = os.path.abspath(os.path.dirname(__file__))
    openapi_json = os.path.normpath(
        os.path.join(
            superset_dir, "..", "..", "docs", "static", "resources", "openapi.json"
        )
    )
    os.makedirs(os.path.dirname(openapi_json), exist_ok=True)

    click.secho(f"Generating {openapi_json}", fg="green")
    with open(openapi_json, "w", encoding="utf-8") as fp:
        json.dump(document, fp, sort_keys=True, indent=2)
        fp.write("\n")
