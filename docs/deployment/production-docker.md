<!--
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
-->

# Production Docker Deployment

Production-friendly deployment of Liteset uses the `lean` Dockerfile target
(asset-baked image, non-root user, uvicorn launcher) plus
`docker-compose-non-dev.yml` for single-host setups or the Helm chart
(`helm/superset/`) for Kubernetes.

## Image targets

| Target  | Purpose                                              | Size (approx) |
|---------|------------------------------------------------------|---------------|
| `lean`  | Production — uvicorn, non-root, assets baked         | 1.2 GB        |
| `dev`   | Local development — `--reload`, source mounted       | 1.7 GB        |
| `ci`    | CI smoke (init + run-server) — `lean` + `[postgres]` | 1.3 GB        |

Build:

```bash
DOCKER_BUILDKIT=1 docker build --target lean -t liteset:lean .
```

Optional translations (compiles `.po` -> `.mo` via `pybabel`):

```bash
DOCKER_BUILDKIT=1 docker build --target lean \
    --build-arg BUILD_TRANSLATIONS=true \
    -t liteset:lean-i18n .
```

## Required secrets

Create `docker/.env-local` (gitignored) and override at least:

| Variable             | Description                                   |
|----------------------|-----------------------------------------------|
| `LITESET_SECRET_KEY` | Random 16+ bytes. `openssl rand -base64 32`.  |
| `DATABASE_PASSWORD`  | Metadata DB password.                         |
| `POSTGRES_PASSWORD`  | Must equal `DATABASE_PASSWORD` if using built-in `db` service. |
| `ADMIN_PASSWORD`     | Bootstrap admin password (default: `admin`). |

> The default `LITESET_SECRET_KEY=TEST_NON_DEV_SECRET_KEY_at_least_16_chars`
> in `docker/.env` is for development only. Production must override it.

For Kubernetes use Helm `extraSecretEnv` or an `existingSecret` reference.

## docker-compose (single host)

```bash
docker compose -f docker-compose-non-dev.yml up -d --build
```

Expected: 5 containers running, `superset_init` exits with `0`. The stack is
ready in 1–2 minutes. Access UI at <http://localhost:8088> with `admin / $ADMIN_PASSWORD`.

Verify health:

```bash
curl -fsS http://localhost:8088/health     # -> OK
docker compose -f docker-compose-non-dev.yml ps
```

Tear down (preserves volumes):

```bash
docker compose -f docker-compose-non-dev.yml down
```

Wipe data:

```bash
docker compose -f docker-compose-non-dev.yml down -v
```

## Healthchecks

- HTTP `/health` (also `/healthcheck`, `/ping`, `/api/v1/health`) — plain `OK`
  text, no auth required.
- HTTP `/healthz` — readiness probe with DB + Redis checks. Returns 200 with
  `{"status":"OK"}` or 503 with `{"status":"ERROR","checks":{...}}`.

K8s liveness/readiness should target `/health`; readiness gates that depend on
DB / Redis should target `/healthz`.

## Helm chart

```bash
helm dependency update helm/superset
helm install superset helm/superset --create-namespace -n superset
```

Critical defaults that production must override (`-f my-values.yaml`):

```yaml
image:
  repository: my-registry/liteset
  tag: "1.0.0"

extraSecretEnv:
  LITESET_SECRET_KEY: "${{ secrets.LITESET_SECRET_KEY }}"

supersetNode:
  connections:
    db_host: postgres.production.svc.cluster.local
    db_user: superset_app
    db_pass: ${{ secrets.DATABASE_PASSWORD }}
    redis_host: redis-master.cache.svc.cluster.local

postgresql:
  enabled: false   # use external managed DB
redis:
  enabled: false   # use external managed Redis
```

Validate before install:

```bash
helm lint helm/superset -f my-values.yaml
helm template superset helm/superset -f my-values.yaml > /tmp/rendered.yaml
```

## Upgrades

Init container runs `superset db upgrade`, `superset init`,
`superset fab create-admin` — all are idempotent. Safe to run on every
deploy.

```bash
docker compose -f docker-compose-non-dev.yml pull   # if using pushed images
docker compose -f docker-compose-non-dev.yml up -d --build
```

Helm:

```bash
helm upgrade superset helm/superset -f my-values.yaml -n superset
```

The Helm `init-job` is annotated `helm.sh/hook: post-install,post-upgrade`
so it re-runs migrations + permission sync automatically.

## Database driver behaviour

A single `SQLALCHEMY_DATABASE_URI` value is used both at runtime
(`postgresql+asyncpg://` via asyncpg) and for Alembic migrations
(`postgresql+psycopg2://` via psycopg2). The driver is rewritten
automatically:

- `superset/config.py:1483` — `SupersetSettings.convert_to_async_driver`
  rewrites the URL to `+asyncpg` for the runtime engine.
- `superset/cli/db.py:58` — `_get_alembic_config` rewrites the URL back
  to `+psycopg2` for the migration CLI.

So both plain `postgresql://`, `postgresql+psycopg2://`, and
`postgresql+asyncpg://` are equivalent in env vars and Helm values.

## Workers and Celery

The lean image ships Celery; bring up workers via:

- compose: `superset-worker` and `superset-worker-beat` services
- Helm: `supersetWorker` / `supersetCeleryBeat` deployments (enabled by default)

Worker command: `celery --app=superset.tasks.celery_app:app worker -O fair`.
Liveness uses `celery inspect ping`.

`supersetCeleryFlower.enabled` is `false` by default — the lean image does
not ship `flower`. To enable, build a custom image with `pip install flower`
or use a sidecar.

## Multi-arch images

Only `linux/amd64` is built and tested. The `node` stage already
honours `--platform=${BUILDPLATFORM}` but the Python stages do not.
To build for arm64, use buildx with QEMU emulation:

```bash
docker buildx create --name liteset --use
docker buildx build --platform linux/arm64 --target lean -t liteset:lean-arm64 .
```

(Untested; expect 5–10× slowdown under emulation.)

## Known limitations

- `docker-compose-image-tag.yml` is **deprecated** for Liteset — it pulls
  upstream `apache/superset` (Flask + gunicorn) images. Use
  `docker-compose-non-dev.yml` instead.
- `superset-websocket/` Node sidecar is **retained** as an optional
  artefact. Native WebSocket exists in Litestar; the sidecar deployment
  (`deployment-ws.yaml` in Helm) is opt-in via `supersetWebsockets.enabled`.
- `helm/superset/templates/deployment-flower.yaml` references the lean
  image which does not ship `flower`. Disabled by default.
- `dockerize.Dockerfile` builds a tiny utility image used by Helm init
  containers for `wait-for-tcp` logic. Built separately:
  `docker build -f dockerize.Dockerfile -t liteset:dockerize .`
