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
"""Tests for ``superset.utils.slack``.

The upstream suite (``tests/unit_tests/utils/slack_test.py``) exercises
``get_channels_with_search`` / ``SlackChannelTypes`` / ``get_slack_client``,
the Slack channel-listing helpers backing the alert/report destination
picker. The Litestar port of ``superset.utils.slack`` only carries
``get_user_avatar`` and ``SlackClientError``; the cached channel-listing
path (which depended on the Flask ``cache_manager`` memoization and the
report ``SlackChannelSchema``) was not ported, so those tests are skipped
below with a factual reason rather than dropped silently.
"""

import pytest

# The channel-search helpers under test do not exist in the port.
slack = pytest.importorskip("superset.utils.slack")

_CHANNEL_SEARCH_MISSING = not all(
    hasattr(slack, name)
    for name in ("get_channels_with_search", "SlackChannelTypes", "get_slack_client")
)

pytestmark = pytest.mark.skipif(
    _CHANNEL_SEARCH_MISSING,
    reason=(
        "superset.utils.slack.get_channels_with_search / SlackChannelTypes / "
        "get_slack_client are not part of the Litestar port (the cached Slack "
        "channel-listing path was not migrated)"
    ),
)


class MockResponse:
    def __init__(self, data):
        self._data = data

    @property
    def data(self):
        return self._data


class TestGetChannelsWithSearch:
    # Fetch all channels when no search string is provided
    def test_fetch_all_channels_no_search_string(self, mocker):
        mock_data = {
            "channels": [{"name": "general", "id": "C12345"}],
            "response_metadata": {"next_cursor": None},
        }
        mock_response_instance = MockResponse(mock_data)
        mock_client = mocker.Mock()
        mock_client.conversations_list.return_value = mock_response_instance
        mocker.patch("superset.utils.slack.get_slack_client", return_value=mock_client)

        result = slack.get_channels_with_search()
        assert result == [{"name": "general", "id": "C12345"}]

    # Handle an empty search string gracefully
    def test_handle_empty_search_string(self, mocker):
        mock_data = {
            "channels": [{"name": "general", "id": "C12345"}],
            "response_metadata": {"next_cursor": None},
        }
        mock_response_instance = MockResponse(mock_data)
        mock_client = mocker.Mock()
        mock_client.conversations_list.return_value = mock_response_instance
        mocker.patch("superset.utils.slack.get_slack_client", return_value=mock_client)

        result = slack.get_channels_with_search(search_string="")
        assert result == [{"name": "general", "id": "C12345"}]

    def test_handle_exact_match_search_string_single_channel(self, mocker):
        mock_data = {
            "channels": [
                {"name": "general", "id": "C12345"},
                {"name": "general2", "id": "C13454"},
                {"name": "random", "id": "C67890"},
            ],
            "response_metadata": {"next_cursor": None},
        }
        mock_response_instance = MockResponse(mock_data)
        mock_client = mocker.Mock()
        mock_client.conversations_list.return_value = mock_response_instance
        mocker.patch("superset.utils.slack.get_slack_client", return_value=mock_client)

        result = slack.get_channels_with_search(
            search_string="general", exact_match=True
        )
        assert result == [{"name": "general", "id": "C12345"}]

    def test_handle_exact_match_search_string_multiple_channels(self, mocker):
        mock_data = {
            "channels": [
                {"name": "general", "id": "C12345"},
                {"name": "general2", "id": "C13454"},
                {"name": "random", "id": "C67890"},
            ],
            "response_metadata": {"next_cursor": None},
        }
        mock_response_instance = MockResponse(mock_data)
        mock_client = mocker.Mock()
        mock_client.conversations_list.return_value = mock_response_instance
        mocker.patch("superset.utils.slack.get_slack_client", return_value=mock_client)

        result = slack.get_channels_with_search(
            search_string="general,random", exact_match=True
        )
        assert result == [
            {"name": "general", "id": "C12345"},
            {"name": "random", "id": "C67890"},
        ]

    def test_handle_loose_match_search_string_multiple_channels(self, mocker):
        mock_data = {
            "channels": [
                {"name": "general", "id": "C12345"},
                {"name": "general2", "id": "C13454"},
                {"name": "random", "id": "C67890"},
            ],
            "response_metadata": {"next_cursor": None},
        }
        mock_response_instance = MockResponse(mock_data)
        mock_client = mocker.Mock()
        mock_client.conversations_list.return_value = mock_response_instance
        mocker.patch("superset.utils.slack.get_slack_client", return_value=mock_client)

        result = slack.get_channels_with_search(search_string="general,random")
        assert result == [
            {"name": "general", "id": "C12345"},
            {"name": "general2", "id": "C13454"},
            {"name": "random", "id": "C67890"},
        ]

    def test_handle_slack_client_error_listing_channels(self, mocker):
        from slack_sdk.errors import SlackApiError

        from superset.exceptions import SupersetException

        mock_client = mocker.Mock()
        mock_client.conversations_list.side_effect = SlackApiError(
            "foo", "missing scope: channels:read"
        )
        mocker.patch("superset.utils.slack.get_slack_client", return_value=mock_client)

        with pytest.raises(SupersetException) as ex:
            slack.get_channels_with_search()

        assert str(ex.value) == (
            """Failed to list channels: foo
The server responded with: missing scope: channels:read"""
        )

    @pytest.mark.parametrize(
        "types, expected_channel_ids",
        [
            (["public_channel"], {"public_channel_id"}),
            (["private_channel"], {"private_channel_id"}),
            (
                ["public_channel", "private_channel"],
                {"public_channel_id", "private_channel_id"},
            ),
            ([], {"public_channel_id", "private_channel_id"}),
        ],
    )
    def test_filter_channels_by_specified_types(
        self, types, expected_channel_ids, mocker
    ):
        mock_data = {
            "channels": [
                {
                    "id": "public_channel_id",
                    "name": "open",
                    "is_member": False,
                    "is_private": False,
                },
                {
                    "id": "private_channel_id",
                    "name": "secret",
                    "is_member": False,
                    "is_private": True,
                },
            ],
            "response_metadata": {"next_cursor": None},
        }
        mock_response_instance = MockResponse(mock_data)
        mock_client = mocker.Mock()
        mock_client.conversations_list.return_value = mock_response_instance
        mocker.patch("superset.utils.slack.get_slack_client", return_value=mock_client)

        # Map raw values onto the port's enum members.
        channel_types = [slack.SlackChannelTypes(t) for t in types]
        result = slack.get_channels_with_search(types=channel_types)
        assert {channel["id"] for channel in result} == expected_channel_ids

    def test_handle_pagination_multiple_pages(self, mocker):
        mock_data_page1 = {
            "channels": [{"name": "general", "id": "C12345"}],
            "response_metadata": {"next_cursor": "page2"},
        }
        mock_data_page2 = {
            "channels": [{"name": "random", "id": "C67890"}],
            "response_metadata": {"next_cursor": None},
        }
        mock_response_instance_page1 = MockResponse(mock_data_page1)
        mock_response_instance_page2 = MockResponse(mock_data_page2)
        mock_client = mocker.Mock()
        mock_client.conversations_list.side_effect = [
            mock_response_instance_page1,
            mock_response_instance_page2,
        ]
        mocker.patch("superset.utils.slack.get_slack_client", return_value=mock_client)

        result = slack.get_channels_with_search()
        assert result == [
            {"name": "general", "id": "C12345"},
            {"name": "random", "id": "C67890"},
        ]
