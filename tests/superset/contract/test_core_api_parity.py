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
"""Contract tests ensuring all core API controllers are registered.

These tests verify that the Litestar app registers all expected routes
from the core API phase, matching the endpoint paths from the Flask API.
"""

from __future__ import annotations

import msgspec.structs
import pytest
from litestar.testing import AsyncTestClient

from superset.controllers.chart import ChartController
from superset.controllers.dashboard import DashboardController
from superset.controllers.dashboard_filter_state import DashboardFilterStateController
from superset.controllers.database import DatabaseController
from superset.controllers.dataset import DatasetController
from superset.controllers.query import QueryController
from superset.controllers.saved_query import SavedQueryController
from superset.controllers.sqllab import SqlLabController
from superset.controllers.sqllab_permalink import SqlLabPermalinkController
from tests.superset.integration.conftest import create_test_app

CORE_CONTROLLERS = [
    ChartController,
    DashboardController,
    DashboardFilterStateController,
    DatabaseController,
    DatasetController,
    QueryController,
    SavedQueryController,
    SqlLabController,
    SqlLabPermalinkController,
]

EXPECTED_PATHS = [
    "/api/v1/chart",
    "/api/v1/dashboard",
    "/api/v1/database",
    "/api/v1/dataset",
    "/api/v1/query",
    "/api/v1/saved_query",
    "/api/v1/sqllab",
]


@pytest.mark.parametrize("controller_cls", CORE_CONTROLLERS)
def test_controller_has_path(controller_cls):
    """Each controller must have a path attribute."""
    assert hasattr(controller_cls, "path"), f"{controller_cls.__name__} missing path"
    assert controller_cls.path.startswith("/api/v1/")


@pytest.mark.parametrize("controller_cls", CORE_CONTROLLERS)
def test_controller_has_tags(controller_cls):
    """Each controller must have tags for OpenAPI grouping."""
    assert hasattr(controller_cls, "tags"), f"{controller_cls.__name__} missing tags"
    assert len(controller_cls.tags) > 0


def test_chart_controller_path():
    assert ChartController.path == "/api/v1/chart"


def test_dashboard_controller_path():
    assert DashboardController.path == "/api/v1/dashboard"


def test_database_controller_path():
    assert DatabaseController.path == "/api/v1/database"


def test_dataset_controller_path():
    assert DatasetController.path == "/api/v1/dataset"


def test_query_controller_path():
    assert QueryController.path == "/api/v1/query"


def test_saved_query_controller_path():
    assert SavedQueryController.path == "/api/v1/saved_query"


def test_sqllab_controller_path():
    assert SqlLabController.path == "/api/v1/sqllab"


def test_sqllab_permalink_controller_path():
    assert SqlLabPermalinkController.path == "/api/v1/sqllab/permalink"


def test_all_expected_paths_covered():
    """Verify all expected API paths have at least one controller."""
    controller_paths = {c.path for c in CORE_CONTROLLERS}
    for expected in EXPECTED_PATHS:
        assert any(
            cp == expected or cp.startswith(expected) for cp in controller_paths
        ), f"No controller for {expected}"


def test_total_controller_count():
    """Verify we have 9 core controllers registered."""
    assert len(CORE_CONTROLLERS) == 9


# ---------------------------------------------------------------------------
# JSON structure parity: verify response schemas match Flask API contracts
# ---------------------------------------------------------------------------

# Flask's BaseSupersetModelRestApi returns these top-level keys.
# Superset must match to keep frontend compatibility.

FLASK_LIST_RESPONSE_KEYS = {
    "count",
    "result",
    "ids",
    "label_columns",
    "list_columns",
    "order_columns",
    "description_columns",
}

FLASK_INFO_RESPONSE_KEYS = {"permissions", "add_columns", "edit_columns", "filters"}

FLASK_RELATED_RESPONSE_KEYS = {"count", "result"}
FLASK_RELATED_ITEM_KEYS = {"value", "text"}

FLASK_DISTINCT_RESPONSE_KEYS = {"count", "result"}
FLASK_DISTINCT_ITEM_KEYS = {"text", "value"}

FLASK_FAVORITE_STATUS_KEYS = {"result"}
FLASK_FAVORITE_ITEM_KEYS = {"id", "value"}

FLASK_ERROR_RESPONSE_KEYS = {"errors", "message"}


def _field_names(struct_instance: object) -> set[str]:
    """Extract field names from a msgspec.Struct instance."""
    return {f.name for f in msgspec.structs.fields(type(struct_instance))}


def test_api_list_response_matches_flask():
    """ApiListResponse struct must emit the same top-level keys as Flask."""
    from superset.schemas.base import ApiListResponse

    assert FLASK_LIST_RESPONSE_KEYS <= _field_names(ApiListResponse(result=[], count=0))


def test_info_response_matches_flask():
    """InfoResponse struct must emit the same keys as Flask /_info."""
    from superset.schemas.base import InfoResponse

    assert FLASK_INFO_RESPONSE_KEYS <= _field_names(InfoResponse())


def test_related_response_matches_flask():
    """RelatedResponse must have count + result with {value, text} items."""
    from superset.schemas.base import RelatedResponse, RelatedResultItem

    assert FLASK_RELATED_RESPONSE_KEYS <= _field_names(RelatedResponse())
    assert FLASK_RELATED_ITEM_KEYS <= _field_names(RelatedResultItem(value=1, text="x"))


def test_distinct_response_matches_flask():
    """DistinctResponse must have count + result with {text, value} items."""
    from superset.schemas.base import DistinctResponse, DistinctResultItem

    assert FLASK_DISTINCT_RESPONSE_KEYS <= _field_names(DistinctResponse())
    assert FLASK_DISTINCT_ITEM_KEYS <= _field_names(
        DistinctResultItem(text="x", value=1)
    )


def test_favorite_status_response_matches_flask():
    """FavoriteStatusResponse must have result with {id, value} items."""
    from superset.schemas.base import FavoriteStatusItem, FavoriteStatusResponse

    assert FLASK_FAVORITE_STATUS_KEYS <= _field_names(FavoriteStatusResponse())
    assert FLASK_FAVORITE_ITEM_KEYS <= _field_names(
        FavoriteStatusItem(id=1, value=True)
    )


def test_error_response_matches_flask():
    """ErrorResponse must have errors + message keys (SIP-40)."""
    from superset.schemas.base import ErrorResponse

    assert FLASK_ERROR_RESPONSE_KEYS <= _field_names(ErrorResponse())


def test_str_param_does_not_shadow_static_routes():
    """Verify /{id_or_uuid:str} does not capture static routes like /_info.

    Litestar resolves literal path segments before parameterized ones,
    so /_info, /export/, /favorite_status/ etc. are matched correctly
    even when /{id_or_uuid:str} exists on the same controller.

    Regression test for C4 review finding.
    """
    from litestar import Controller, get, Litestar
    from litestar.testing import TestClient

    class _TestCtrl(Controller):
        path = "/api"

        @get("/_info")
        async def info(self) -> dict[str, str]:
            return {"handler": "info"}

        @get("/export/")
        async def export(self) -> dict[str, str]:
            return {"handler": "export"}

        @get("/{id_or_uuid:str}")
        async def get_one(self, id_or_uuid: str) -> dict[str, str]:
            return {"handler": "get_one", "id": id_or_uuid}

    app = Litestar([_TestCtrl])
    with TestClient(app) as client:
        assert client.get("/api/_info").json()["handler"] == "info"
        assert client.get("/api/export/").json()["handler"] == "export"
        assert client.get("/api/some-uuid").json()["handler"] == "get_one"


# ---------------------------------------------------------------------------
# M11 — Response-structure parity: list endpoints must return Flask contract keys
# ---------------------------------------------------------------------------

# Flask's BaseSupersetModelRestApi list endpoints always include at minimum
# ``result`` (list of items) and ``count`` (total rows) at the top level.
# These are the keys the Superset frontend relies on for pagination.

RESPONSE_STRUCTURE = [
    (ChartController, "/api/v1/chart/", {"result", "count"}),
    (DashboardController, "/api/v1/dashboard/", {"result", "count"}),
    (DatabaseController, "/api/v1/database/", {"result", "count"}),
    (DatasetController, "/api/v1/dataset/", {"result", "count"}),
    (QueryController, "/api/v1/query/", {"result", "count"}),
    (SavedQueryController, "/api/v1/saved_query/", {"result", "count"}),
]


@pytest.mark.parametrize("ctrl,path,expected_keys", RESPONSE_STRUCTURE)
async def test_response_structure_matches_flask_contract(
    ctrl: type, path: str, expected_keys: set[str]
) -> None:
    """Each list endpoint must include the keys the Flask API emits.

    The Superset frontend reads ``result`` and ``count`` from every list
    response.  A missing key would silently break pagination or empty-state
    rendering in the UI.
    """
    app = create_test_app(ctrl)
    async with AsyncTestClient(app=app) as client:
        resp = await client.get(path)
        assert resp.status_code == 200, (
            f"{ctrl.__name__} GET {path} returned {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert expected_keys <= set(body.keys()), (
            f"{ctrl.__name__} response missing keys "
            f"{expected_keys - set(body.keys())!r}; got {set(body.keys())!r}"
        )
