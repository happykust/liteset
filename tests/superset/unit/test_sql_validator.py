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
"""Pure-logic unit tests for the SQL Lab validators.

Ported 1:1 (in intent) from the vendored upstream integration test
``tests/integration_tests/sql_validator_tests.py``. The validators live at
``superset.sql.validators`` in the Liteset port (upstream:
``superset.sql_validators``).

The validate paths exercised here do not touch the database or the example
data: the database is a :class:`~unittest.mock.MagicMock`. The original test
patched ``superset.utils.core.g`` / ``g.user.username`` defensively, but
neither validator's ``validate`` path reads the current user, so those
patches are dropped.

The Presto path imports ``pyhive`` lazily and the PostgreSQL path shells out
to the ``ecpg`` binary via ``pgsanity``; both are optional in CI, so the
relevant tests skip when those dependencies are unavailable.
"""

import shutil
from unittest.mock import MagicMock

import pytest

from superset.sql.validators.postgres import PostgreSQLValidator
from superset.sql.validators.presto_db import (
    PrestoDBSQLValidator,
    PrestoSQLValidationError,
)

PRESTO_ERROR_TEMPLATE = {
    "errorLocation": {"lineNumber": 10, "columnNumber": 20},
    "message": "your query isn't how I like it",
}


class TestPrestoValidator:
    """Testing for the prestodb sql validator."""

    @pytest.fixture
    def db_error_cls(self):
        """``pyhive`` is an optional engine dependency; the Presto validator
        imports ``pyhive.exc`` lazily. Skip when it is not installed."""
        pyhive_exc = pytest.importorskip(
            "pyhive.exc",
            reason="pyhive is required by the Presto validator",
        )
        return pyhive_exc.DatabaseError

    @pytest.fixture
    def database(self):
        """Mirror the original ``setUp``: a MagicMock database wired so that
        ``get_sqla_engine(...).__enter__().raw_connection().cursor()`` returns
        a cursor whose ``poll()`` short-circuits the validation loop."""
        database = MagicMock()
        database_engine = database.get_sqla_engine.return_value.__enter__.return_value
        database_conn = database_engine.raw_connection.return_value
        database_cursor = database_conn.cursor.return_value
        database_cursor.poll.return_value = None
        return database

    def test_validator_success(self, database, db_error_cls):
        sql = "SELECT 1 FROM default.notarealtable"
        schema = "default"

        errors = PrestoDBSQLValidator.validate(sql, None, schema, database)

        assert errors == []

    def test_validator_db_error(self, database, db_error_cls):
        sql = "SELECT 1 FROM default.notarealtable"
        schema = "default"

        fetch_fn = database.db_engine_spec.fetch_data
        fetch_fn.side_effect = db_error_cls("dummy db error")

        with pytest.raises(PrestoSQLValidationError):
            PrestoDBSQLValidator.validate(sql, None, schema, database)

    def test_validator_unexpected_error(self, database, db_error_cls):
        sql = "SELECT 1 FROM default.notarealtable"
        schema = "default"

        fetch_fn = database.db_engine_spec.fetch_data
        fetch_fn.side_effect = Exception("a mysterious failure")

        with pytest.raises(Exception):  # noqa: B017, PT011
            PrestoDBSQLValidator.validate(sql, None, schema, database)

    def test_validator_query_error(self, database, db_error_cls):
        sql = "SELECT 1 FROM default.notarealtable"
        schema = "default"

        fetch_fn = database.db_engine_spec.fetch_data
        fetch_fn.side_effect = db_error_cls(PRESTO_ERROR_TEMPLATE)

        errors = PrestoDBSQLValidator.validate(sql, None, schema, database)

        assert len(errors) == 1


# ``pgsanity`` shells out to the ``ecpg`` PostgreSQL preprocessor binary, which
# is not installed in all environments. Skip the suite when it is unavailable.
ecpg_missing = pytest.mark.skipif(
    shutil.which("ecpg") is None,
    reason="the 'ecpg' binary (pgsanity backend) is not installed",
)


@ecpg_missing
class TestPostgreSQLValidator:
    def test_valid_syntax(self):
        mock_database = MagicMock()
        annotations = PostgreSQLValidator.validate(
            sql='SELECT 1, "col" FROM "table"',
            catalog=None,
            schema="",
            database=mock_database,
        )
        assert annotations == []

    def test_invalid_syntax(self):
        mock_database = MagicMock()
        annotations = PostgreSQLValidator.validate(
            sql='SELECT 1, "col"\nFROOM "table"',
            catalog=None,
            schema="",
            database=mock_database,
        )

        assert len(annotations) == 1
        annotation = annotations[0]
        assert annotation.line_number == 2
        assert annotation.start_column is None
        assert annotation.end_column is None
        assert annotation.message == 'ERROR: syntax error at or near """'
