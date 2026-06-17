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
import pytest

from superset.schemas.annotation import (
    AnnotationLayerPostSchema,
    AnnotationLayerPutSchema,
    AnnotationPostSchema,
    AnnotationPutSchema,
)


def test_annotation_layer_post_descr_null_accepted():
    """POST with descr=null must be accepted.

    ``descr`` is ``String(allow_none=True)`` so JSON null is valid.
    Regression: bare ``str`` type rejects JSON null → 422 instead of 201.
    """
    body = msgspec.json.decode(
        b'{"name": "My Layer", "descr": null}',
        type=AnnotationLayerPostSchema,
    )
    assert body.name == "My Layer"
    assert body.descr is None


def test_annotation_layer_post_descr_string_accepted():
    body = msgspec.json.decode(
        b'{"name": "My Layer", "descr": "some description"}',
        type=AnnotationLayerPostSchema,
    )
    assert body.descr == "some description"


def test_annotation_layer_post_descr_omitted_defaults():
    """descr omitted from body stays UNSET (column keeps SQL NULL).

    ``descr = fields.String(allow_none=True)`` with no ``missing`` omits the
    absent field from the loaded dict — it must NOT default to ``""``.
    """
    body = msgspec.json.decode(
        b'{"name": "My Layer"}',
        type=AnnotationLayerPostSchema,
    )
    assert body.descr is msgspec.UNSET


def test_annotation_layer_post_name_required():
    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(
            b'{"descr": "no name here"}',
            type=AnnotationLayerPostSchema,
        )


def test_annotation_layer_post_name_min_length():
    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(
            b'{"name": ""}',
            type=AnnotationLayerPostSchema,
        )


def test_annotation_layer_post_name_max_length():
    long_name = "x" * 251
    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(
            f'{{"name": "{long_name}"}}'.encode(),
            type=AnnotationLayerPostSchema,
        )


def test_annotation_post_short_descr_max_length_enforced():
    """short_descr must not exceed 500 characters.

    ``validate=[Length(1, 500)]`` is required; without max_length the DB
    column (String(500)) raises a data-too-long error → 500 instead of 422.
    """
    too_long = "x" * 501
    payload = (
        f'{{"short_descr": "{too_long}",'
        f' "start_dttm": "2024-01-01T00:00:00",'
        f' "end_dttm": "2024-01-02T00:00:00"}}'
    ).encode()
    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(payload, type=AnnotationPostSchema)


def test_annotation_post_short_descr_max_length_boundary_accepted():
    at_limit = "x" * 500
    payload = (
        f'{{"short_descr": "{at_limit}",'
        f' "start_dttm": "2024-01-01T00:00:00",'
        f' "end_dttm": "2024-01-02T00:00:00"}}'
    ).encode()
    body = msgspec.json.decode(payload, type=AnnotationPostSchema)
    assert len(body.short_descr) == 500


def test_annotation_post_short_descr_min_length():
    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(
            b'{"short_descr": "",'
            b' "start_dttm": "2024-01-01T00:00:00",'
            b' "end_dttm": "2024-01-02T00:00:00"}',
            type=AnnotationPostSchema,
        )


def test_annotation_post_valid():
    body = msgspec.json.decode(
        b'{"short_descr": "My annotation",'
        b' "start_dttm": "2024-01-01T00:00:00",'
        b' "end_dttm": "2024-01-02T00:00:00",'
        b' "long_descr": "Details here",'
        b' "json_metadata": null}',
        type=AnnotationPostSchema,
    )
    assert body.short_descr == "My annotation"
    assert body.long_descr == "Details here"
    assert body.json_metadata is None


def test_annotation_layer_put_partial():
    body = msgspec.json.decode(
        b'{"name": "Updated Name"}',
        type=AnnotationLayerPutSchema,
    )
    assert body.name == "Updated Name"
    assert body.descr is msgspec.UNSET


def test_annotation_layer_put_descr_null_rejected():
    """PUT with descr=null must be rejected (no allow_none=True on this field).

    ``AnnotationLayerPutSchema.descr = fields.String(required=False)`` —
    Marshmallow 3.x defaults ``allow_none=False``, so ``{"descr": null}`` yields 422.
    Regression: liteset used ``str | None | UNSET`` which silently accepted
    null instead of rejecting it.
    """
    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(
            b'{"name": "A", "descr": null}',
            type=AnnotationLayerPutSchema,
        )


def test_annotation_put_partial():
    body = msgspec.json.decode(
        b'{"short_descr": "New title"}',
        type=AnnotationPutSchema,
    )
    assert body.short_descr == "New title"
    assert body.long_descr is msgspec.UNSET


def test_annotation_layer_post_descr_over_250_accepted():
    """POST descr longer than 250 chars must be accepted.

    ``descr = fields.String(allow_none=True)`` has no Length validator; the
    DB column is ``Column(Text)``, unlimited.  Regression: liteset added
    ``Meta(max_length=250)`` to descr (copied from name) causing 422 for
    long descriptions.
    """
    long_descr = "x" * 251
    payload = f'{{"name": "Layer", "descr": "{long_descr}"}}'.encode()
    body = msgspec.json.decode(payload, type=AnnotationLayerPostSchema)
    assert len(body.descr) == 251


def test_annotation_layer_put_descr_over_250_accepted():
    """PUT descr longer than 250 chars must be accepted.

    ``descr = fields.String(required=False)`` has no Length validator.
    Same regression as the POST case above.
    """
    long_descr = "x" * 251
    payload = f'{{"descr": "{long_descr}"}}'.encode()
    body = msgspec.json.decode(payload, type=AnnotationLayerPutSchema)
    assert len(body.descr) == 251


def test_annotation_post_long_descr_absent_defaults_none():
    """Absent long_descr must default to None (maps to NULL in DB).

    ``long_descr = fields.String(allow_none=True)`` with no ``missing``
    argument — absent field is omitted from the loaded dict so the column
    keeps its SQL default (NULL).  Regression: liteset default was ``""``
    which stored an empty string instead of NULL.
    """
    body = msgspec.json.decode(
        b'{"short_descr": "s", "start_dttm": "2024-01-01T00:00:00",'
        b' "end_dttm": "2024-01-02T00:00:00"}',
        type=AnnotationPostSchema,
    )
    assert body.long_descr is msgspec.UNSET


def test_annotation_post_json_metadata_absent_defaults_none():
    """Absent json_metadata must default to None (maps to NULL in DB).

    ``json_metadata = fields.String(allow_none=True)`` with no ``missing``
    argument — absent field is omitted from the loaded dict.
    Regression: liteset default was ``""`` which stored an empty string.
    """
    body = msgspec.json.decode(
        b'{"short_descr": "s", "start_dttm": "2024-01-01T00:00:00",'
        b' "end_dttm": "2024-01-02T00:00:00"}',
        type=AnnotationPostSchema,
    )
    assert body.json_metadata is msgspec.UNSET
