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
"""Unit tests for the ``load_chart_data_into_cache`` Celery task.

The Liteset port builds the query context via the module-level helper
``_create_query_context_from_form`` (the async replacement for the original
``ChartDataQueryContextSchema().load(form_data)``) and publishes job status
through the synchronous ``_update_job`` Redis-Streams helper. The intent
mirrors upstream: when query-context construction fails, the task must mark
the job failed (status ``error`` with the error message) and re-raise.
"""

from unittest import mock

import pytest

from superset.commands.chart.exceptions import ChartDataQueryFailedError
from superset.tasks import async_queries


def _noop_gettext(message: str) -> str:
    """Local no-op replacement for ``flask_babel.lazy_gettext``."""
    return message


@mock.patch("superset.db.session.get_sync_session")
@mock.patch.object(async_queries, "_update_job")
@mock.patch.object(async_queries, "_create_query_context_from_form")
def test_load_chart_data_into_cache_with_error(
    mock_create_query_context,
    mock_update_job,
    mock_get_sync_session,
) -> None:
    """Test that the task is gracefully marked failed in event of error."""
    from superset.tasks.async_queries import load_chart_data_into_cache

    job_metadata = {"user_id": 1}
    form_data: dict = {}
    err_message = "Something went wrong"
    err = ChartDataQueryFailedError(_noop_gettext(err_message))

    mock_get_sync_session.return_value = mock.MagicMock()
    mock_create_query_context.side_effect = err

    with pytest.raises(ChartDataQueryFailedError):
        load_chart_data_into_cache(job_metadata, form_data)

    expected_errors = [{"message": err_message}]

    mock_update_job.assert_called_once_with(
        job_metadata, async_queries.STATUS_ERROR, errors=expected_errors
    )
