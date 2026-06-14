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
"""Flask-free port of the vendored upstream
``tests/unit_tests/datasets/schema_tests.py``.

Liteset does not use Marshmallow; the ``python_date_format`` validation lives
on ``AsyncDatasetDAO.validate_python_date_format`` (a 1:1 port of the upstream
``superset_old/daos/dataset.py`` helper) and returns a ``bool`` instead of
raising ``marshmallow.ValidationError``.  The two upstream parametrize lists
(accepted / rejected formats) are preserved verbatim; only the assertion shape
changes from ``raises(ValidationError)`` to ``is False``.
"""

import pytest

from superset.db.daos.dataset import AsyncDatasetDAO


@pytest.mark.parametrize(
    "payload",
    [
        "epoch_ms",
        "epoch_s",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y%m%d",
    ],
)
def test_validate_python_date_format(payload) -> None:
    assert AsyncDatasetDAO.validate_python_date_format(payload) is True


@pytest.mark.parametrize(
    "payload",
    [
        "%d%m%Y",
        "%Y/%m/%dT%H:%M:%S.%f",
    ],
)
def test_validate_python_date_format_rejects(payload) -> None:
    assert AsyncDatasetDAO.validate_python_date_format(payload) is False
