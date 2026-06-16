#!/usr/bin/env bash

#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
set -e

# Liteset integration tests run against a REAL Postgres backend. The schema is
# created by the Alembic migrations (``superset db upgrade``) and example data
# is seeded by the real ``superset.examples`` loaders — both driven by the
# session-scoped ``integration_backend`` fixture in
# tests/superset/integration/conftest.py. DB-backed tests ``pytest.skip`` when
# LITESET_SQLALCHEMY_DATABASE_URI is unset or not Postgres, so the workflow MUST
# provide a Postgres URI via that variable.
export LITESET_SECRET_KEY="${LITESET_SECRET_KEY:-test-secret-key-at-least-32-bytes-long-xx}"
export LITESET_TESTENV=true

if [[ "${LITESET_SQLALCHEMY_DATABASE_URI:-}" != *postgresql* ]]; then
  echo "ERROR: LITESET_SQLALCHEMY_DATABASE_URI must point at Postgres for integration tests." >&2
  echo "       e.g. postgresql+asyncpg://superset:superset@127.0.0.1:15432/superset" >&2
  exit 1
fi

echo "Liteset integration DB: ${LITESET_SQLALCHEMY_DATABASE_URI}"
echo "Running integration tests"

pytest \
  --durations-min=2 \
  --cov-report= \
  --cov=superset \
  -p no:cacheprovider \
  --continue-on-collection-errors \
  ./tests/superset/integration "$@"
