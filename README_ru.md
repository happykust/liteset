<!--
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
-->

# Liteset

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/license/apache-2-0)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![Litestar](https://img.shields.io/badge/Litestar-2.15+-7c3aed.svg)](https://litestar.dev/)
[![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0-red.svg)](https://www.sqlalchemy.org/)
[![Based on Apache Superset](https://img.shields.io/badge/based%20on-Apache%20Superset%206.0.0-blue.svg)](https://github.com/apache/superset)

**Liteset — асинхронный порт Apache Superset, переписанный с Flask/WSGI на Litestar/ASGI.**

Проект сохраняет полную обратную совместимость с существующими инсталляциями Apache Superset: схема БД метаданных, HTTP API, WebSocket-контракт и фронтенд остаются неизменными. Достаточно остановить Superset, установить Liteset поверх той же базы данных — и продолжить работу с теми же дашбордами, датасетами, пользователями и ролями.

---

## Оглавление

- [Мотивация](#мотивация)
- [Целевая архитектура](#целевая-архитектура)
- [Технологический стек](#технологический-стек)
- [Гарантии совместимости](#гарантии-совместимости)
- [Структура проекта](#структура-проекта)
- [Установка и запуск](#установка-и-запуск)
- [Лицензия](#лицензия)

---

## Мотивация

Исторически Apache Superset построен на Flask/WSGI и запускается через Gunicorn с пре-форком процессов. Такая модель имеет три фундаментальных ограничения:

1. **Блокирующий ввод-вывод.** При длительных запросах к аналитическим СУБД поток выполнения простаивает в ожидании ответа, не обслуживая другие запросы.
2. **Высокое потребление памяти.** Каждый worker-процесс копирует всё приложение и держит собственный пул соединений к БД метаданных.
3. **Ограниченный параллелизм.** Количество одновременных запросов жёстко ограничено числом процессов × потоков.

Liteset устраняет эти узкие места, переводя весь веб-слой на асинхронную модель ASGI. Ожидаемый эффект — прирост RPS в 2-3 раза на IO-bound-нагрузках и существенное снижение резидентной памяти за счёт перехода с пре-форка процессов на один event loop.

---

## Целевая архитектура

Серверная часть Liteset построена по принципам Clean Architecture. Приложение разделено на четыре слоя; зависимости направлены строго внутрь — внутренние слои не импортируют из внешних.

| Слой | Ответственность | Реализация |
|---|---|---|
| **Presentation** | Контроллеры, DTO, сериализация, авторизационные предикаты | `superset/controllers/`, `superset/schemas/`, `superset/guards/` — `async def` обработчики Litestar, DTO на `msgspec.Struct`, Guards для RBAC |
| **Business Logic** | Бизнес-правила (`validate() → run()`) | `superset/commands/` — `AsyncBaseCommand`, фреймворк-независимые классы Command |
| **Data Access** | Доступ к данным через SQLAlchemy 2.0 Select API | `superset/db/base_dao.py`, `superset/db/daos/` — `BaseAsyncDAO[T]` с `AsyncSession` в конструкторе |
| **Infrastructure** | Middleware, DI, конфигурация, движок БД | `superset/middleware/`, `superset/dependencies.py`, `superset/config.py` |

---

## Технологический стек

| Категория | Компонент | Роль                                                                   |
|---|---|------------------------------------------------------------------------|
| **ASGI-фреймворк** | [Litestar](https://litestar.dev/) | Маршрутизация, DI, OpenAPI, Guards, Middleware                         |
| **ASGI-сервер** | Uvicorn + uvloop | Event loop на базе libuv                                               |
| **ORM** | SQLAlchemy 2.0 (Async) | Declarative-модели, запросы к БД                                       |
| **Драйвер метаданных** | asyncpg / aiosqlite | Асинхронный доступ к БД метаданных                                     |
| **Сериализация** | [msgspec](https://jcristharif.com/msgspec/) | DTO + валидация, заменяет Marshmallow и Pydantic v1                    |
| **Конфигурация** | pydantic-settings | Типизированная конфигурация с backward-compat для `superset_config.py` |
| **Миграции** | Alembic (psycopg2, sync) | Схема БД наследуется 1-в-1 от Superset 6.0.0                           |
| **Фоновые задачи** | Celery | Оставлен без изменений (ортогонален HTTP-слою)                         |
| **WebSocket** | Нативный Litestar | Заменяет отдельный Node.js-сервис `superset-websocket`                 |
| **Кэш** | Redis (redis-py async) | Per-request cache, auth user cache, async events                       |
| **Логирование** | structlog | Структурированные JSON-логи                                            |

---

## Гарантии совместимости

Liteset — это **drop-in replacement** для Apache Superset 6.0.0 на уровне backend-а. Фиксируются три инварианта:

### 1. БД метаданных

Схема таблиц метаданных (`ab_user`, `ab_role`, `dashboards`, `slices`, `tables`, `dbs`, `query`, `saved_query`, `report_schedule` и т.д.) наследуется без изменений. Alembic-ревизии перенесены целиком. Существующая инсталляция Superset может быть мигрирована простой подменой бекенда — без `superset db upgrade`.

### 2. Фронтенд

**Код фронтенда (`superset-frontend/`) запрещено изменять.** Liteset обязан воспроизводить все эндпоинты, форматы JSON-ответов, cookie-формат сессии (Flask-подписанные session cookies декодируются нативно), CSRF-токены (`X-CSRFToken`), rison-параметры запросов и SPA-шаблон `/superset/welcome`.

### 3. HTTP API

Все 37+ REST-контроллеров воспроизводят контракт Superset 1:1 — URL-маршруты, коды ответа, имена полей (поддерживается двойной `camelCase`/`snake_case` lookup на стороне msgspec), структура пагинации, ошибки SIP-40, формат Swagger-спеки. OpenAPI-документация автогенерируется на `/swagger/v1`.

---

## Структура проекта

```
liteset/
├── superset/                       # Async backend на Litestar
│   ├── app.py                      # Фабрика Litestar-приложения
│   ├── config.py                   # SupersetSettings (pydantic-settings)
│   ├── dependencies.py             # DI Provide'ы (session, user, security_manager)
│   ├── exceptions.py               # Иерархия SIP-40 + handlers
│   ├── controllers/                # Presentation layer — 37 контроллеров
│   │   ├── base.py                 # RISON-хелперы, пагинация, сериализация
│   │   ├── chart.py, dashboard.py, database.py, dataset.py, …
│   │   └── sqllab.py, report.py, security.py, user.py, …
│   ├── commands/                   # Business Logic layer
│   │   ├── base.py                 # AsyncBaseCommand (validate/run)
│   │   └── chart.py, dashboard.py, database.py, …
│   ├── db/
│   │   ├── session.py              # AsyncEngine, async_sessionmaker
│   │   ├── base_dao.py             # BaseAsyncDAO[T]
│   │   ├── daos/                   # Data Access layer
│   │   │   ├── chart.py, dashboard.py, database.py, …
│   │   │   └── security.py, user.py, …
│   │   └── engine_specs/           # Async DB адаптеры
│   │       ├── base.py             # BaseAsyncEngineSpec
│   │       ├── postgres.py         # Нативный asyncpg
│   │       ├── mysql.py            # Нативный asyncmy
│   │       ├── clickhouse.py       # aiochclient
│   │       ├── trino.py            # aiotrino
│   │       └── sync_fallback.py    # Обёртка для СУБД через conn.run_sync()
│   ├── guards/                     # RBAC Guards
│   ├── middleware/                 # Auth, CSRF, locale, security headers, proxy fix
│   ├── schemas/                    # DTO на msgspec.Struct
│   ├── security/                   # AsyncSecurityManager (порт FAB)
│   ├── async_events/               # Redis-streams-based async events
│   ├── websocket/                  # Нативный Litestar WebSocket
│   ├── common/                     # QueryContext/QueryObject
│   ├── models/                     # SQLAlchemy 2.0 declarative models
│   ├── migrations/                 # Alembic (psycopg2, sync)
│   ├── db_engine_specs/            # Sync BaseEngineSpec (для SQL-диалектов)
│   ├── sql/                        # SQL parser, Jinja templating
│   ├── viz.py                      # Legacy viz engine (explore_json)
│   └── static/, templates/         # SPA bundle, Jinja-шаблоны
├── superset-frontend/              # React-фронтенд (не модифицируется)
├── tests/                          # pytest
├── requirements/                   # base.in, development.in, …
└── pyproject.toml
```

---

## Установка и запуск

Ознакомьтесь с [руководством](https://liteset.happykust.dev/docs/quickstart/) Liteset или изучите [варианты развёртывания в продуктивной среде](https://liteset.happykust.dev/docs/installation/architecture/).

---

## Лицензия

Liteset распространяется под лицензией [Apache License 2.0](LICENSE.txt), наследуя её от Apache Superset. Все заимствованные файлы из Apache Superset 6.0.0 сохраняют оригинальные ASF-хедеры.

---

<p>
  <em>Liteset — это академический порт, автор проекта мог пропустить важные детали, которые вызовут регрессию относительно Apache Superset 6.0.0. Для production-инсталляций Apache Superset по-прежнему используйте <a href="https://github.com/apache/superset">apache/superset</a>.</em>
</p>
