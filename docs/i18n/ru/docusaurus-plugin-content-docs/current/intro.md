---
hide_title: true
sidebar_position: 1
---

# Liteset

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/license/apache-2-0)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![Litestar](https://img.shields.io/badge/Litestar-2.15+-7c3aed.svg)](https://litestar.dev/)
[![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0-red.svg)](https://www.sqlalchemy.org/)
[![Based on Apache Superset](https://img.shields.io/badge/based%20on-Apache%20Superset%206.0.0-blue.svg)](https://github.com/apache/superset)

**Liteset — это асинхронный порт Apache Superset, переписанный с Flask/WSGI на Litestar/ASGI.**

Проект сохраняет полную обратную совместимость с существующими установками Apache Superset: схема БД метаданных, HTTP API, контракт WebSocket и фронтенд остаются неизменными. Останавливаешь Superset, ставишь Liteset на ту же базу — и продолжаешь работать с теми же дашбордами, датасетами, пользователями и ролями.

---

## Содержание

- [Мотивация](#мотивация)
- [Целевая архитектура](#целевая-архитектура)
- [Технологический стек](#технологический-стек)
- [Гарантии совместимости](#гарантии-совместимости)
- [Структура проекта](#структура-проекта)
- [Установка и запуск](#установка-и-запуск)
- [Тестирование](#тестирование)
- [Лицензия](#лицензия)

---

## Мотивация

Исторически Apache Superset построен на Flask/WSGI и работает под Gunicorn с pre-forked процессами. У такой модели три фундаментальных ограничения:

1. **Блокирующий I/O.** Во время длинного запроса к аналитической БД воркер сидит idle и не обслуживает другие запросы.
2. **Высокий memory footprint.** Каждый воркер несёт копию всего приложения и собственный пул соединений к БД метаданных.
3. **Ограниченный параллелизм.** Число одновременных запросов жёстко ограничено `процессы × потоки`.

Liteset убирает эти узкие места, переводя весь веб-слой на async ASGI-модель. Ожидаемый результат — рост RPS в 2–3 раза на IO-bound нагрузках и ощутимое снижение резидентной памяти за счёт перехода с pre-forked процессов на единый event loop.

---

## Целевая архитектура

Сервер Liteset спроектирован по принципам Clean Architecture. Приложение разбито на четыре слоя; зависимости идут строго внутрь — внутренние слои никогда не импортируют внешние.

| Слой | Ответственность | Реализация |
|---|---|---|
| **Presentation** | Контроллеры, DTO, сериализация, авторизационные предикаты | `superset/controllers/`, `superset/schemas/`, `superset/guards/` — `async def` хендлеры Litestar, DTO на `msgspec.Struct`, Guards для RBAC |
| **Business Logic** | Бизнес-правила (`validate() → run()`) | `superset/commands/` — `AsyncBaseCommand`, framework-независимые Command-классы |
| **Data Access** | Доступ к данным через SQLAlchemy 2.0 Select API | `superset/db/base_dao.py`, `superset/db/daos/` — `BaseAsyncDAO[T]` с `AsyncSession` в конструкторе |
| **Infrastructure** | Middleware, DI, конфигурация, DB engine | `superset/middleware/`, `superset/dependencies.py`, `superset/config.py` |

---

## Технологический стек

| Категория | Компонент | Роль |
|---|---|---|
| **ASGI-фреймворк** | [Litestar](https://litestar.dev/) | Роутинг, DI, OpenAPI, Guards, Middleware |
| **ASGI-сервер** | Uvicorn + uvloop | Event loop на libuv |
| **ORM** | SQLAlchemy 2.0 (Async) | Декларативные модели, запросы к БД |
| **Драйвер метаданных** | asyncpg / aiosqlite | Async-доступ к БД метаданных |
| **Сериализация** | [msgspec](https://jcristharif.com/msgspec/) | DTO + валидация, заменяет Marshmallow и Pydantic v1 |
| **Конфигурация** | pydantic-settings | Типизированная конфигурация, совместимая с `superset_config.py` |
| **Миграции** | Alembic (psycopg2, sync) | Схема БД унаследована 1:1 от Superset 6.0.0 |
| **Фоновые задачи** | Celery | Не меняется (ортогональна HTTP-слою) |
| **WebSocket** | Native Litestar | Заменяет отдельный Node.js-сервис `superset-websocket` |
| **Кэш** | Redis (redis-py async) | Per-request cache, кэш auth-пользователей, async events |
| **Логирование** | structlog | Структурный JSON-лог |

---

## Гарантии совместимости

Liteset — это **drop-in замена** Apache Superset 6.0.0 на уровне бэкенда. Зафиксированы три инварианта:

### 1. БД метаданных

Схема таблиц метаданных (`ab_user`, `ab_role`, `dashboards`, `slices`, `tables`, `dbs`, `query`, `saved_query`, `report_schedule` и др.) унаследована без изменений. Alembic-ревизии перенесены целиком. Существующая инсталляция Superset мигрируется простой подменой бэкенда — без `superset db upgrade`.

### 2. Фронтенд

**Код фронтенда (`superset-frontend/`) не модифицируется.** Liteset обязан воспроизводить каждый endpoint, форму JSON-ответов, формат session cookie (Flask-подписанные cookie декодируются нативно), CSRF-токены (`X-CSRFToken`), rison-параметры и SPA-шаблон `/superset/welcome`.

### 3. HTTP API

Все REST-контроллеры воспроизводят контракт Superset 1:1 — URL-маршруты, коды ответа, имена полей (поддерживается двойной поиск `camelCase`/`snake_case` на стороне msgspec), shape пагинации, SIP-40 ошибки, layout Swagger-спеки. OpenAPI-документация авто-генерируется на `/swagger/v1`.

---

## Структура проекта

```
liteset/
├── superset/                       # Async-бэкенд на Litestar
│   ├── app.py                      # Application factory Litestar
│   ├── config.py                   # SupersetSettings (pydantic-settings)
│   ├── dependencies.py             # DI Provide's (session, user, security_manager)
│   ├── exceptions.py               # SIP-40 иерархия + handlers
│   ├── controllers/                # Presentation — 37 контроллеров
│   ├── commands/                   # Business Logic
│   ├── db/                         # Data Access (DAO, engine specs)
│   ├── guards/                     # RBAC Guards
│   ├── middleware/                 # Auth, CSRF, locale, security headers, proxy fix
│   ├── schemas/                    # DTO на msgspec.Struct
│   ├── security/                   # AsyncSecurityManager (порт FAB)
│   ├── async_events/               # Async events на Redis Streams
│   ├── websocket/                  # Native Litestar WebSocket
│   ├── common/                     # QueryContext / QueryObject
│   ├── models/                     # SQLAlchemy 2.0 declarative
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

См. [Быстрый старт](/ru/docs/quickstart) или [варианты продакшен-деплоя](/ru/docs/installation/architecture).

---

## Тестирование

Liteset проходит сравнительное тестирование с Apache Superset 6.0.0:

- [Методика](/ru/docs/benchmarks/methodology)
- [Результаты](/ru/docs/benchmarks/results)
- [Сравнение с Apache Superset](/ru/docs/benchmarks/comparison)

---

## Лицензия

Liteset распространяется под [Apache License 2.0](https://apache.org/licenses/LICENSE-2.0), унаследованной у Apache Superset. Все файлы, перенесённые из Apache Superset 6.0.0, сохраняют оригинальные ASF-заголовки.

---

<p>
  <em>Liteset — академический порт; автор мог упустить детали, приводящие к регрессиям относительно Apache Superset 6.0.0. Для продакшен-инсталляций используйте <a href="https://github.com/apache/superset">apache/superset</a>.</em>
</p>
