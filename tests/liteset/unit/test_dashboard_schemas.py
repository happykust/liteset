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
from __future__ import annotations

import msgspec

from liteset.schemas.dashboard import (
    DashboardColorsUpdateSchema,
    DashboardCopySchema,
    DashboardGetResponse,
    DashboardJSONMetadata,
    DashboardPostSchema,
    DashboardPutSchema,
    DashboardScreenshotSchema,
    EmbeddedDashboardConfig,
    ImportV1Dashboard,
)


def test_dashboard_post_body():
    body = msgspec.json.decode(
        b'{"dashboard_title": "My Dashboard", "published": true, "owners": [1, 2]}',
        type=DashboardPostSchema,
    )
    assert body.dashboard_title == "My Dashboard"
    assert body.published is True
    assert body.owners == [1, 2]
    assert body.slug is None
    assert body.css is None
    assert body.is_managed_externally is False


def test_dashboard_post_body_without_title():
    """dashboard_title is optional — omitting it should succeed."""
    body = msgspec.json.decode(
        b'{"published": true}',
        type=DashboardPostSchema,
    )
    assert body.dashboard_title is None
    assert body.published is True
    assert body.slug is None


def test_dashboard_put_body_partial():
    body = msgspec.json.decode(
        b'{"dashboard_title": "Updated Title"}',
        type=DashboardPutSchema,
    )
    assert body.dashboard_title == "Updated Title"
    assert body.slug is msgspec.UNSET
    assert body.published is msgspec.UNSET
    assert body.owners is msgspec.UNSET


def test_dashboard_copy_body():
    body = msgspec.json.decode(
        b'{"dashboard_title": "Copy", "json_metadata": "{}", "duplicate_slices": true, "css": ".my-class {}"}',
        type=DashboardCopySchema,
    )
    assert body.dashboard_title == "Copy"
    assert body.duplicate_slices is True
    assert body.css == ".my-class {}"
    assert body.json_metadata == "{}"


def test_dashboard_get_response_roundtrip():
    resp = DashboardGetResponse(
        id=42, result={"dashboard_title": "Test", "published": True}
    )
    encoded = msgspec.json.encode(resp)
    decoded = msgspec.json.decode(encoded, type=DashboardGetResponse)
    assert decoded.id == 42
    assert decoded.result["dashboard_title"] == "Test"
    assert decoded.message is None


def test_embedded_dashboard_config():
    cfg = EmbeddedDashboardConfig(allowed_domains=["https://example.com"])
    assert cfg.allowed_domains == ["https://example.com"]

    cfg_empty = EmbeddedDashboardConfig()
    assert cfg_empty.allowed_domains == []


def test_import_v1_dashboard():
    payload = msgspec.json.decode(
        b'{"dashboard_title": "Imported", "uuid": "abc-123", "position": {"ROOT_ID": {"type": "ROOT"}}}',
        type=ImportV1Dashboard,
    )
    assert payload.dashboard_title == "Imported"
    assert payload.uuid == "abc-123"
    assert payload.position == {"ROOT_ID": {"type": "ROOT"}}
    assert payload.version == "1.0.0"
    assert payload.is_managed_externally is False
    assert payload.description is None


def test_dashboard_colors_update_with_label_colors():
    body = msgspec.json.decode(
        b'{"label_colors": {"series1": "#ff0000"}, "color_scheme_domain": ["#ff0000"]}',
        type=DashboardColorsUpdateSchema,
    )
    assert body.label_colors == {"series1": "#ff0000"}
    assert body.color_scheme_domain == ["#ff0000"]


def test_import_v1_dashboard_full():
    body = msgspec.json.decode(
        b'{"uuid": "abc-123", "dashboard_title": "T", "certified_by": "Admin", "published": true, "tags": ["tag1"]}',
        type=ImportV1Dashboard,
    )
    assert body.certified_by == "Admin"
    assert body.tags == ["tag1"]


def test_screenshot_body_url_params_tuple_format():
    body = msgspec.json.decode(
        b'{"urlParams": [["key1", "val1"], ["key2", "val2"]]}',
        type=DashboardScreenshotSchema,
    )
    assert body.urlParams == [["key1", "val1"], ["key2", "val2"]]


def test_dashboard_json_metadata_stagger():
    meta = msgspec.json.decode(
        b'{"stagger_refresh": true, "stagger_time": 10, "filter_bar_orientation": "VERTICAL"}',
        type=DashboardJSONMetadata,
    )
    assert meta.stagger_refresh is True
    assert meta.filter_bar_orientation == "VERTICAL"
