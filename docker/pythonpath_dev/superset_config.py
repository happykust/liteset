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
#
# Liteset (async Superset) docker development configuration.
# Loaded by SupersetSettings via SUPERSET_CONFIG_PATH env var.
# Uses plain Python values only — no Flask-specific extensions.
import logging
import os
import sys

logger = logging.getLogger()

DATABASE_DIALECT = os.getenv("DATABASE_DIALECT", "postgresql")
DATABASE_USER = os.getenv("DATABASE_USER", "superset")
DATABASE_PASSWORD = os.getenv("DATABASE_PASSWORD", "superset")
DATABASE_HOST = os.getenv("DATABASE_HOST", "db")
DATABASE_PORT = os.getenv("DATABASE_PORT", "5432")
DATABASE_DB = os.getenv("DATABASE_DB", "superset")

EXAMPLES_USER = os.getenv("EXAMPLES_USER", "examples")
EXAMPLES_PASSWORD = os.getenv("EXAMPLES_PASSWORD", "examples")
EXAMPLES_HOST = os.getenv("EXAMPLES_HOST", "db")
EXAMPLES_PORT = os.getenv("EXAMPLES_PORT", "5432")
EXAMPLES_DB = os.getenv("EXAMPLES_DB", "examples")

# The SQLAlchemy connection string.  SupersetSettings.convert_to_async_driver
# rewrites bare ``postgresql://`` to ``postgresql+asyncpg://`` automatically.
SQLALCHEMY_DATABASE_URI = (
    f"{DATABASE_DIALECT}://"
    f"{DATABASE_USER}:{DATABASE_PASSWORD}@"
    f"{DATABASE_HOST}:{DATABASE_PORT}/{DATABASE_DB}"
)

SQLALCHEMY_EXAMPLES_URI = (
    f"{DATABASE_DIALECT}://"
    f"{EXAMPLES_USER}:{EXAMPLES_PASSWORD}@"
    f"{EXAMPLES_HOST}:{EXAMPLES_PORT}/{EXAMPLES_DB}"
)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
# Dedicated logical Redis DBs per purpose so the keyspaces stay isolated:
# flushing one (e.g. the Celery result backend) never wipes another (e.g.
# the auth/user cache or the data cache).
REDIS_CELERY_DB = os.getenv("REDIS_CELERY_DB", "0")  # Celery broker
REDIS_RESULTS_DB = os.getenv("REDIS_RESULTS_DB", "1")  # Celery result backend
REDIS_CACHE_DB = os.getenv("REDIS_CACHE_DB", "2")  # app / data / thumbnail cache
REDIS_AUTH_DB = os.getenv("REDIS_AUTH_DB", "3")  # auth user cache + async events

# Async Redis URL used by the Litestar app for the auth/user cache, async
# query events, rate limiting and the cache_manager fallback.  Isolated on
# its own DB (REDIS_AUTH_DB), separate from Celery and the data cache.
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_AUTH_DB}"

# Cache configuration — superset.cache.manager understands the original
# Flask-Caching ``CACHE_TYPE`` strings ("redis"/"RedisCache",
# "simple"/"SimpleCache", "null"/"NullCache").  Flask-Caching itself is no
# longer a dependency; the manager builds a redis client / in-memory dict
# directly based on these knobs.  Lives on REDIS_CACHE_DB, separate from
# the Celery result backend.
CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_DEFAULT_TIMEOUT": 300,
    "CACHE_KEY_PREFIX": "superset_",
    "CACHE_REDIS_HOST": REDIS_HOST,
    "CACHE_REDIS_PORT": REDIS_PORT,
    "CACHE_REDIS_DB": REDIS_CACHE_DB,
}
DATA_CACHE_CONFIG = CACHE_CONFIG
THUMBNAIL_CACHE_CONFIG = CACHE_CONFIG


class CeleryConfig:
    broker_url = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_CELERY_DB}"
    imports = (
        "superset.tasks.scheduler",
        "superset.tasks.thumbnails",
        "superset.tasks.cache",
        "superset.tasks.async_queries",
    )
    result_backend = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_RESULTS_DB}"
    worker_prefetch_multiplier = 1
    task_acks_late = False
    beat_schedule = {
        "reports.scheduler": {
            "task": "reports.scheduler",
            "schedule": 60.0,  # every minute
        },
        "reports.prune_log": {
            "task": "reports.prune_log",
            "schedule": 86400.0,  # daily
        },
    }


CELERY_CONFIG = CeleryConfig

FEATURE_FLAGS = {"ALERT_REPORTS": True}
ALERT_REPORTS_NOTIFICATION_DRY_RUN = True
WEBDRIVER_BASEURL = (
    f"http://superset_app{os.environ.get('SUPERSET_APP_ROOT', '/')}/"
)
WEBDRIVER_BASEURL_USER_FRIENDLY = (
    f"http://localhost:8088{os.environ.get('SUPERSET_APP_ROOT', '/')}/"
)
SQLLAB_CTAS_NO_LIMIT = True

log_level_text = os.getenv("SUPERSET_LOG_LEVEL", "INFO")
LOG_LEVEL = getattr(logging, log_level_text.upper(), logging.INFO)

if os.getenv("CYPRESS_CONFIG") == "true":
    # When running the service as a cypress backend, we need to import the config
    # located @ tests/integration_tests/superset_test_config.py
    base_dir = os.path.dirname(__file__)
    module_folder = os.path.abspath(
        os.path.join(base_dir, "../../tests/integration_tests/")
    )
    sys.path.insert(0, module_folder)
    from superset_test_config import *  # noqa: F401, F403

    sys.path.pop(0)

#
# Optionally import superset_config_docker.py (which will have been included on
# the PYTHONPATH) in order to allow for local settings to be overridden
#
try:
    import superset_config_docker
    from superset_config_docker import *  # noqa: F401, F403

    logger.info(
        "Loaded your Docker configuration at [%s]", superset_config_docker.__file__
    )
except ImportError:
    logger.info("Using default Docker config...")
