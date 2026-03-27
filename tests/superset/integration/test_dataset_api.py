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
"""Integration tests for DatasetController.

Tests the full HTTP pipeline with mocked DAO dependencies.
"""

from __future__ import annotations

import pytest
from litestar.testing import AsyncTestClient

from superset.controllers.dataset import DatasetController
from tests.superset.integration.conftest import create_test_app, create_test_app_no_auth


@pytest.fixture
def app():
    return create_test_app(DatasetController)


async def test_get_dataset_list(app):
    """GET /api/v1/dataset/ returns empty list with count."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/dataset/")
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data
        assert "count" in data
        assert data["result"] == []
        assert data["count"] == 0


async def test_get_dataset_info(app):
    """GET /api/v1/dataset/_info returns permissions metadata."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/dataset/_info")
        assert resp.status_code == 200
        data = resp.json()
        assert "permissions" in data
        assert "can_read" in data["permissions"]


async def test_get_dataset_by_id_not_found(app):
    """GET /api/v1/dataset/{pk} returns 404 when not found."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/dataset/999")
        assert resp.status_code == 404


async def test_delete_dataset_bulk_no_ids(app):
    """DELETE /api/v1/dataset/ (bulk) with no ids returns 422 validation error."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.delete("/api/v1/dataset/")
        # rison_params mock returns None → ids=[] → CommandInvalidError
        assert resp.status_code == 422


async def test_update_dataset_not_found(app):
    """PUT /api/v1/dataset/{pk} returns 404 when dataset is not found.

    MockDAO.find_by_id returns None, so UpdateDatasetCommand raises
    ObjectNotFoundError -> 404.
    """
    async with AsyncTestClient(app=app) as client:
        resp = await client.put(
            "/api/v1/dataset/999",
            json={"table_name": "updated_table"},
        )
        assert resp.status_code == 404


async def test_delete_dataset_single_not_found(app):
    """DELETE /api/v1/dataset/{pk} returns 404 when dataset not found."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.delete("/api/v1/dataset/999")
        assert resp.status_code == 404


async def test_duplicate_dataset_not_found(app):
    """POST /api/v1/dataset/duplicate returns 404 when base dataset not found.

    MockDAO.find_by_id returns None, so DuplicateDatasetCommand raises
    ObjectNotFoundError -> 404.
    """
    async with AsyncTestClient(app=app) as client:
        resp = await client.post(
            "/api/v1/dataset/duplicate",
            json={"base_model_id": 999, "table_name": "duplicate_table"},
        )
        assert resp.status_code == 404


async def test_unauthenticated_returns_401():
    """GET /api/v1/dataset/ without credentials returns 401."""
    no_auth_app = create_test_app_no_auth(DatasetController)
    async with AsyncTestClient(app=no_auth_app) as client:
        resp = await client.get("/api/v1/dataset/")
        assert resp.status_code == 401
