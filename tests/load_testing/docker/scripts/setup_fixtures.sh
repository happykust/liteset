#!/bin/bash
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

# Setup fixtures in Superset for load testing
# This script:
# 1. Waits for Superset to be ready
# 2. Creates database connections
# 3. Creates datasets, charts, and dashboards

set -e

SUPERSET_URL="${SUPERSET_URL:-http://localhost:8088}"
SUPERSET_USERNAME="${SUPERSET_USERNAME:-admin}"
SUPERSET_PASSWORD="${SUPERSET_PASSWORD:-admin}"

CLICKHOUSE_URI="${CLICKHOUSE_URI:-clickhousedb://default:@clickhouse:8123/loadtest}"
POSTGRES_URI="${POSTGRES_URI:-postgresql+psycopg2://loadtest:loadtest@postgres:5432/loadtest}"
MYSQL_URI="${MYSQL_URI:-mysql+pymysql://loadtest:loadtest@mysql:3306/loadtest}"

echo "=========================================="
echo "SUPERSET FIXTURE SETUP"
echo "=========================================="
echo "Superset URL: ${SUPERSET_URL}"
echo "Username: ${SUPERSET_USERNAME}"
echo ""

# Wait for Superset to be ready
echo "Waiting for Superset to be ready..."
max_attempts=60
attempt=0
while [ $attempt -lt $max_attempts ]; do
    if curl -s "${SUPERSET_URL}/health" | grep -q "OK"; then
        echo "Superset is ready!"
        break
    fi
    attempt=$((attempt + 1))
    echo "  Attempt $attempt/$max_attempts - waiting..."
    sleep 5
done

if [ $attempt -eq $max_attempts ]; then
    echo "ERROR: Superset did not become ready in time"
    exit 1
fi

# Change to the fixtures directory
cd "$(dirname "$0")/../.."

# Install dependencies
echo ""
echo "Installing Python dependencies..."
pip install -q requests

# Run fixture setup
echo ""
echo "Running fixture setup..."
python -m fixtures.setup \
    --url "${SUPERSET_URL}" \
    --username "${SUPERSET_USERNAME}" \
    --password "${SUPERSET_PASSWORD}" \
    --clickhouse "${CLICKHOUSE_URI}" \
    --postgres "${POSTGRES_URI}" \
    --mysql "${MYSQL_URI}"

echo ""
echo "=========================================="
echo "FIXTURE SETUP COMPLETE"
echo "=========================================="
echo ""
echo "You can now run load tests with:"
echo "  locust -f locustfile.py --host=${SUPERSET_URL}"
echo ""
