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
"""Unit tests for the sync Celery task helper ``_update_job``.

The relay reads the per-channel Redis Stream directly, so ``_update_job`` must
write both the channel and global firehose streams and must NOT publish to
pub/sub (the legacy real-time notification path that was removed).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from superset.tasks import async_queries


def test_update_job_writes_both_streams_no_publish():
    """``_update_job`` writes both streams and does not call ``publish``."""
    sync_redis = MagicMock()

    with patch.object(async_queries, "_get_sync_redis", return_value=sync_redis):
        async_queries._update_job(
            {"channel_id": "ch-1", "job_id": "job-1", "user_id": 42},
            async_queries.STATUS_DONE,
            result_url="/api/v1/chart/data/cache-key-123",
        )

    # Two xadd calls: channel stream then global firehose stream.
    assert sync_redis.xadd.call_count == 2
    channel_call, global_call = sync_redis.xadd.call_args_list
    assert channel_call.args[0] == "async-events-ch-1"
    assert global_call.args[0] == "async-events-full"

    # The payload carries the id-bearing event data shape under "data".
    payload = channel_call.args[1]
    event = json.loads(payload["data"])
    assert event["channel_id"] == "ch-1"
    assert event["job_id"] == "job-1"
    assert event["user_id"] == 42
    assert event["status"] == async_queries.STATUS_DONE
    assert event["result_url"] == "/api/v1/chart/data/cache-key-123"

    # Pub/sub publish has been removed.
    sync_redis.publish.assert_not_called()
