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
"""Slack integration utilities.

Provides avatar lookup via the Slack API. Requires ``slack_sdk``
to be installed (optional dependency).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


class SlackClientError(Exception):
    """Error communicating with the Slack API."""


def get_user_avatar(email: str) -> str:
    """Look up a Slack user's 192px profile image by email.

    Raises :class:`SlackClientError` on any failure.
    """
    try:
        from slack_sdk import WebClient
    except ImportError as exc:
        raise SlackClientError(
            "slack_sdk is not installed — cannot fetch avatars"
        ) from exc

    token = os.environ.get("SLACK_API_TOKEN", "")
    if callable(token):
        token = token()
    if not token:
        raise SlackClientError("SLACK_API_TOKEN is not configured")

    client = WebClient(token=token)

    try:
        response = client.users_lookupByEmail(email=email)
    except Exception as exc:
        raise SlackClientError(f"Failed to lookup user by email: {email}") from exc

    user = response.data.get("user")
    if user is None:
        raise SlackClientError("No user found with that email.")

    profile = user.get("profile")
    if profile is None:
        raise SlackClientError("User found but no profile available.")

    avatar_url = profile.get("image_192")
    if avatar_url is None:
        raise SlackClientError("Profile image is not available.")

    return avatar_url
