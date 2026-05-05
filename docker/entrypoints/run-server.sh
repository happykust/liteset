#!/usr/bin/env bash
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#
# Production-grade uvicorn launcher for the async Superset (Litestar) app.
# Replaces the legacy gunicorn launcher used by the Flask version.

SUPERSET_APP="${SUPERSET_APP:-superset.app:create_app}"

exec uvicorn "${SUPERSET_APP}" \
    --factory \
    --host "${SUPERSET_BIND_ADDRESS:-0.0.0.0}" \
    --port "${SUPERSET_PORT:-8088}" \
    --workers "${SERVER_WORKER_AMOUNT:-4}" \
    --loop "${UVICORN_LOOP:-uvloop}" \
    --http "${UVICORN_HTTP:-httptools}" \
    --log-level "${UVICORN_LOGLEVEL:-info}" \
    --timeout-keep-alive "${UVICORN_KEEPALIVE:-5}" \
    --proxy-headers \
    --forwarded-allow-ips="${UVICORN_FORWARDED_ALLOW_IPS:-*}"
