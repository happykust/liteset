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
"""OAuth2 state encoding/decoding utilities."""

from __future__ import annotations

import base64
import json


def encode_oauth2_state(state: dict[str, object]) -> str:
    """Encode a state dict to a URL-safe base64 string."""
    payload = json.dumps(state, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def decode_oauth2_state(state: str) -> dict[str, object]:
    """Decode a URL-safe base64 string back to a state dict.

    Raises ``ValueError`` if the string is not valid base64 or JSON.
    """
    try:
        payload = base64.urlsafe_b64decode(state.encode("ascii"))
        result = json.loads(payload)
    except Exception as exc:
        raise ValueError(f"Invalid OAuth2 state: {exc}") from exc
    if not isinstance(result, dict):
        raise ValueError("OAuth2 state must be a JSON object")
    return result
