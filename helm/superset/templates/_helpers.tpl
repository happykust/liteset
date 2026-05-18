{{/*

 Licensed to the Apache Software Foundation (ASF) under one or more
 contributor license agreements.  See the NOTICE file distributed with
 this work for additional information regarding copyright ownership.
 The ASF licenses this file to You under the Apache License, Version 2.0
 (the "License"); you may not use this file except in compliance with
 the License.  You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.

*/}}

{{/* vim: set filetype=mustache: */}}
{{/*
Expand the name of the chart.
*/}}
{{- define "superset.name" -}}
  {{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "superset.fullname" -}}
  {{- if .Values.fullnameOverride -}}
    {{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
  {{- else -}}
    {{- $name := default .Chart.Name .Values.nameOverride -}}
    {{- if contains $name .Release.Name -}}
      {{- .Release.Name | trunc 63 | trimSuffix "-" -}}
    {{- else -}}
      {{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
    {{- end -}}
  {{- end -}}
{{- end -}}

{{/*
Create the name of the service account to use
*/}}
{{- define "superset.serviceAccountName" -}}
  {{- if .Values.serviceAccount.create -}}
    {{- default (include "superset.fullname" .) .Values.serviceAccountName -}}
  {{- else -}}
    {{- default "default" .Values.serviceAccountName -}}
  {{- end -}}
{{- end -}}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "superset.chart" -}}
  {{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}


{{- define "superset-config" }}
# Liteset superset_config.py — rendered by the Helm chart.
#
# Notes for Liteset (vs upstream Apache Superset):
#   * The SQLAlchemy URI uses the plain ``postgresql://`` scheme.
#     ``SupersetSettings.convert_to_async_driver`` (superset/config.py)
#     rewrites it to ``postgresql+asyncpg://`` for the runtime engine,
#     and ``superset.cli.db._get_alembic_config`` rewrites it back to
#     ``postgresql+psycopg2://`` for Alembic migrations.
#   * ``flask_caching`` is no longer a dependency. ``CACHE_CONFIG`` /
#     ``DATA_CACHE_CONFIG`` are plain dicts consumed by
#     ``superset.cache.manager`` which speaks the original Flask-Caching
#     ``CACHE_TYPE`` vocabulary (RedisCache / SimpleCache / NullCache /
#     SupersetMetastoreCache).
#   * ``RESULTS_BACKEND`` must expose ``.get()`` / ``.set()`` /
#     ``.delete()`` synchronously (SQL Lab calls it via
#     ``asyncio.to_thread``). We build a ``SyncRedisCacheAdapter`` from
#     ``superset.cache.manager`` wrapping a stdlib ``redis.Redis``
#     client — fully Liteset-native, no flask_caching import.
import os

from redis import Redis

from superset.cache.manager import SyncRedisCacheAdapter


def env(key, default=None):
    return os.getenv(key, default)


# Redis Base URL
{{- if .Values.supersetNode.connections.redis_password }}
REDIS_BASE_URL = f"{env('REDIS_PROTO')}://{env('REDIS_USER', '')}:{env('REDIS_PASSWORD')}@{env('REDIS_HOST')}:{env('REDIS_PORT')}"
{{- else }}
REDIS_BASE_URL = f"{env('REDIS_PROTO')}://{env('REDIS_HOST')}:{env('REDIS_PORT')}"
{{- end }}

# Redis URL Params
{{- if .Values.supersetNode.connections.redis_ssl.enabled }}
REDIS_URL_PARAMS = f"?ssl_cert_reqs={env('REDIS_SSL_CERT_REQS')}"
{{- else }}
REDIS_URL_PARAMS = ""
{{- end}}

# Build Redis URLs
CACHE_REDIS_URL = f"{REDIS_BASE_URL}/{env('REDIS_DB', 1)}{REDIS_URL_PARAMS}"
CELERY_REDIS_URL = f"{REDIS_BASE_URL}/{env('REDIS_CELERY_DB', 0)}{REDIS_URL_PARAMS}"

MAPBOX_API_KEY = env("MAPBOX_API_KEY", "")

# Plain-dict cache config — parsed by superset.cache.manager.
CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_DEFAULT_TIMEOUT": 300,
    "CACHE_KEY_PREFIX": "superset_",
    "CACHE_REDIS_URL": CACHE_REDIS_URL,
}
DATA_CACHE_CONFIG = CACHE_CONFIG

# Plain ``postgresql://`` — Liteset rewrites the driver itself.
SQLALCHEMY_DATABASE_URI = (
    f"postgresql://{env('DB_USER')}:{env('DB_PASS')}"
    f"@{env('DB_HOST')}:{env('DB_PORT')}/{env('DB_NAME')}"
)


# Celery config (read by superset/tasks/celery_app.py).
# Imports must reference Liteset task modules — upstream's
# ``superset.sql_lab`` does not exist here. ``superset.tasks.*`` is the
# canonical location for scheduled / async tasks.
class CeleryConfig:
    imports = (
        "superset.tasks.scheduler",
        "superset.tasks.thumbnails",
        "superset.tasks.cache",
        "superset.tasks.async_queries",
    )
    broker_url = CELERY_REDIS_URL
    result_backend = CELERY_REDIS_URL


CELERY_CONFIG = CeleryConfig

# Sync Redis cache adapter for SQL Lab results.
# Connects via the same ``redis://`` URL string as CACHE_REDIS_URL so SSL,
# auth and DB selection all stay consistent with CACHE_CONFIG.
RESULTS_BACKEND = SyncRedisCacheAdapter(
    Redis.from_url(CACHE_REDIS_URL),
    default_ttl=86400,
    key_prefix="superset_results_",
)

{{ if .Values.configOverrides }}
# Overrides
{{- range $key, $value := .Values.configOverrides }}
# {{ $key }}
{{ tpl $value $ }}
{{- end }}
{{- end }}

{{ if .Values.configOverridesFiles }}
# Overrides from files
{{- $files := .Files }}
{{- range $key, $value := .Values.configOverridesFiles }}
# {{ $key }}
{{ $files.Get $value }}
{{- end }}
{{- end }}

{{- end }}

{{- define "supersetCeleryBeat.selectorLabels" -}}
app: {{ include "superset.name" . }}-celerybeat
release: {{ .Release.Name }}
{{- end }}

{{- define "supersetCeleryFlower.selectorLabels" -}}
app: {{ include "superset.name" . }}-flower
release: {{ .Release.Name }}
{{- end }}

{{- define "supersetNode.selectorLabels" -}}
app: {{ include "superset.name" . }}
release: {{ .Release.Name }}
{{- end }}

{{- define "supersetWebsockets.selectorLabels" -}}
app: {{ include "superset.name" . }}-ws
release: {{ .Release.Name }}
{{- end }}

{{- define "supersetWorker.selectorLabels" -}}
app: {{ include "superset.name" . }}-worker
release: {{ .Release.Name }}
{{- end }}
