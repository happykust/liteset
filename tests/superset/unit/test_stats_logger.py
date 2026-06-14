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
"""Flask-free port of ``tests/integration_tests/stats_logger_tests.py``.

Verifies that :class:`StatsdStatsLogger` forwards ``incr``/``decr``/
``gauge``/``timing`` calls 1:1 to the underlying statsd client, both
when an explicit client is injected and when one is constructed from
the host/port params.
"""

from unittest.mock import Mock, patch

from superset.stats_logger import StatsdStatsLogger


def _verify_client_calls(logger: StatsdStatsLogger, client: Mock) -> None:
    logger.incr("foo1")
    client.incr.assert_called_once()
    client.incr.assert_called_with("foo1")
    logger.decr("foo2")
    client.decr.assert_called_once()
    client.decr.assert_called_with("foo2")
    logger.gauge("foo3", 2.21)
    client.gauge.assert_called_once()
    client.gauge.assert_called_with("foo3", 2.21)
    logger.timing("foo4", 1.234)
    client.timing.assert_called_once()
    client.timing.assert_called_with("foo4", 1.234)


def test_init_with_statsd_client() -> None:
    client = Mock()
    stats_logger = StatsdStatsLogger(statsd_client=client)
    _verify_client_calls(stats_logger, client)


def test_init_with_params() -> None:
    with patch("superset.stats_logger.StatsClient") as MockStatsdClient:  # noqa: N806
        mock_client = MockStatsdClient.return_value

        stats_logger = StatsdStatsLogger()
        _verify_client_calls(stats_logger, mock_client)
