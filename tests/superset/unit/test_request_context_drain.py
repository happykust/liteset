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
"""Tests for RequestContextMiddleware body draining + replay.

Regression guard: an oversized non-multipart body must still reach the
handler in full (the middleware previously drained-and-discarded it,
leaving downstream handlers with an empty body).
"""

from __future__ import annotations

from superset.middleware import request_context as rc
from superset.middleware.request_context import RequestContextMiddleware


def _make_receive(events):
    it = iter(events)

    async def receive():
        return next(it)

    return receive


async def _drain_all(replay):
    """Pull events from a replay receive until ``more_body`` is False."""
    collected = b""
    while True:
        event = await replay()
        if event.get("type") != "http.request":
            break
        collected += event.get("body") or b""
        if not event.get("more_body"):
            break
    return collected


async def test_small_body_round_trips_intact():
    events = [
        {"type": "http.request", "body": b"hello ", "more_body": True},
        {"type": "http.request", "body": b"world", "more_body": False},
    ]
    body, replay = await RequestContextMiddleware._drain_body(_make_receive(events))
    assert body == b"hello world"
    assert await _drain_all(replay) == b"hello world"


async def test_oversized_body_streams_full_payload_to_handler(monkeypatch):
    # Shrink the cap so we don't need a real 4 MiB payload.
    monkeypatch.setattr(rc, "_MAX_PARSED_BODY_BYTES", 8)
    events = [
        {"type": "http.request", "body": b"AAAA", "more_body": True},
        {"type": "http.request", "body": b"BBBBB", "more_body": True},  # crosses cap
        {"type": "http.request", "body": b"CCCC", "more_body": False},
    ]
    body, replay = await RequestContextMiddleware._drain_body(_make_receive(events))
    # Too large to parse for request context...
    assert body is None
    # ...but the handler still receives the COMPLETE upload.
    assert await _drain_all(replay) == b"AAAABBBBBCCCC"


async def test_disconnect_yields_no_parsed_body():
    events = [
        {"type": "http.request", "body": b"partial", "more_body": True},
        {"type": "http.disconnect"},
    ]
    body, replay = await RequestContextMiddleware._drain_body(_make_receive(events))
    assert body is None
