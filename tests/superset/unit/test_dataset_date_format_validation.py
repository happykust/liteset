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
"""Tests for ``AsyncDatasetDAO.validate_python_date_format``.

Liteset does not use Marshmallow; the validation returns a ``bool`` instead of
raising ``marshmallow.ValidationError``.  The parametrize lists
(accepted / rejected formats) match upstream; only the assertion shape
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
