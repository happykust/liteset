# Apache Superset Load Testing Suite

Комплексное нагрузочное тестирование для Apache Superset на основе Locust.

## 📋 Обзор

Этот набор тестов позволяет провести полноценное нагрузочное тестирование Apache Superset, включая:

- **Dashboard API** — просмотр, фильтрация, экспорт дашбордов
- **Chart Data API** — запросы данных для различных типов визуализаций
- **SQL Lab** — выполнение SQL запросов (sync/async)
- **Explore** — исследование данных и создание чартов
- **Datasets/Databases** — работа с источниками данных

## 🏗️ Архитектура

```
tests/load_testing/
├── locustfile.py           # Главный файл Locust
├── config/
│   ├── settings.py         # Конфигурация тестов
│   └── databases.py        # Настройки БД
├── scenarios/
│   ├── auth.py             # Аутентификация
│   ├── dashboards.py       # Дашборды
│   ├── charts.py           # Чарты и данные
│   ├── sqllab.py           # SQL Lab
│   ├── explore.py          # Explore
│   ├── datasets.py         # Датасеты
│   ├── databases.py        # Базы данных
│   └── mixed.py            # Смешанные workflow
├── utils/
│   ├── api_client.py       # HTTP клиент
│   ├── helpers.py          # Вспомогательные функции
│   └── metrics.py          # Сбор метрик
├── data/
│   └── generators/         # Генераторы тестовых данных
└── docker/
    ├── docker-compose.yml  # Тестовое окружение
    ├── clickhouse/         # ClickHouse инициализация
    ├── postgres/           # PostgreSQL инициализация
    └── mysql/              # MySQL инициализация
```

## 🚀 Быстрый старт

### 1. Запуск тестового окружения

```bash
cd tests/load_testing/docker

# Запуск всех сервисов
docker-compose up -d

# Генерация тестовых данных (10-100GB)
docker-compose --profile generate-data up data-generator

# Проверка статуса
docker-compose ps
```

### 2. Инициализация Superset

```bash
# Инициализация базы данных
docker-compose exec superset superset db upgrade

# Создание admin пользователя
docker-compose exec superset superset fab create-admin \
    --username admin \
    --firstname Admin \
    --lastname User \
    --email admin@example.com \
    --password admin

# Загрузка примеров (опционально)
docker-compose exec superset superset load_examples

# Инициализация
docker-compose exec superset superset init
```

### 3. Создание тестовых данных в Superset (Fixtures)

```bash
# Автоматическое создание дашбордов, чартов, датасетов
cd tests/load_testing
python -m fixtures.setup \
    --url http://localhost:8088 \
    --username admin \
    --password admin \
    --clickhouse "clickhousedb://default:@localhost:8123/loadtest" \
    --postgres "postgresql+psycopg2://loadtest:loadtest@localhost:5432/loadtest" \
    --mysql "mysql+pymysql://loadtest:loadtest@localhost:3306/loadtest"

# Или через Docker
docker-compose exec superset bash /app/docker/scripts/setup_fixtures.sh
```

Скрипт fixtures автоматически создаёт:
- **3 подключения к БД** (ClickHouse, PostgreSQL, MySQL)
- **15+ датасетов** на основе таблиц
- **50+ чартов** разных типов (timeseries, bar, pie, table, pivot, big number)
- **14 дашбордов** (4 тематических + 10 для стресс-тестирования)

### 4. Запуск нагрузочных тестов

```bash
# С веб-интерфейсом
locust -f locustfile.py --host=http://localhost:8088

# Headless режим
locust -f locustfile.py --host=http://localhost:8088 \
    --headless -u 100 -r 10 -t 30m

# Распределённый режим (master)
locust -f locustfile.py --master --host=http://localhost:8088

# Распределённый режим (worker)
locust -f locustfile.py --worker --master-host=localhost
```

### 5. Использование Docker для Locust

```bash
# Запуск Locust через docker-compose
docker-compose up locust-master locust-worker

# Веб-интерфейс доступен на http://localhost:8089
```

## ⚙️ Конфигурация

### Переменные окружения

| Переменная | Описание | По умолчанию |
|------------|----------|--------------|
| `SUPERSET_URL` | URL Superset | `http://localhost:8088` |
| `SUPERSET_USERNAME` | Логин | `admin` |
| `SUPERSET_PASSWORD` | Пароль | `admin` |
| `LOAD_PROFILE` | Профиль нагрузки | `load` |
| `CACHE_MODE` | Режим кэширования | `mixed` |
| `REDIS_HOST` | Redis хост | `localhost` |
| `CLICKHOUSE_HOST` | ClickHouse хост | `localhost` |
| `POSTGRES_HOST` | PostgreSQL хост | `localhost` |
| `MYSQL_HOST` | MySQL хост | `localhost` |

### Профили нагрузки

| Профиль | Users | Spawn Rate | Длительность | Назначение |
|---------|-------|------------|--------------|------------|
| `smoke` | 10 | 1/s | 5 мин | Валидация |
| `load` | 100 | 10/s | 30 мин | Стандартная нагрузка |
| `stress` | 500 | 50/s | 15 мин | Стресс-тест |
| `spike` | 1000 | 100/s | 5 мин | Пиковая нагрузка |
| `soak` | 50 | 5/s | 4 часа | Длительный тест |

### Режимы кэширования

- `enabled` — все запросы используют кэш
- `disabled` — все запросы обходят кэш (force=true)
- `mixed` — 70% с кэшем, 30% без кэша

## 📊 Типы пользователей

### DashboardViewerUser (вес: 40%)
Типичный бизнес-пользователь:
- Просмотр дашбордов
- Применение фильтров
- Экспорт данных

### ChartAnalystUser (вес: 25%)
Аналитик данных:
- Запросы данных чартов
- Pivot таблицы
- Timeseries визуализации

### SQLLabUser (вес: 20%)
Data Engineer:
- SQL запросы (простые и сложные)
- Async запросы
- Исследование схем БД

### PowerUser (вес: 10%)
Продвинутый пользователь:
- Комплексные workflow
- Создание чартов/дашбордов
- Интенсивная работа с SQL Lab

### APIStressUser (вес: 5%)
Стресс-тестирование API:
- Быстрые последовательные запросы
- Тестирование rate limiting

## 📈 Метрики

### Собираемые метрики

- **Response Time** — p50, p75, p90, p95, p99
- **Throughput** — RPS по endpoint'ам
- **Error Rate** — процент ошибок
- **Cache Hit Ratio** — эффективность кэша
- **DB Query Time** — время выполнения запросов в БД
- **Async Query Time** — время асинхронных запросов

### Экспорт результатов

После завершения теста метрики автоматически экспортируются в:
- `./metrics_output/metrics_<timestamp>.json`
- `./metrics_output/metrics_<timestamp>.csv`

## 🗄️ Тестовые базы данных

### ClickHouse (аналитика)
- **events** — 100M+ записей событий
- **metrics** — 50M+ записей метрик
- **user_sessions** — сессии пользователей

### PostgreSQL (OLTP)
- **sales** — 10M+ записей продаж
- **users** — 1M пользователей
- **products** — 10K продуктов
- **events** — события с JSONB

### MySQL (дополнительный источник)
- **orders** — 500K заказов
- **order_items** — 1M позиций
- **customers** — 100K клиентов

## 🔧 Генерация тестовых данных

### Объёмы данных

| Таблица | Целевой объём | ~Размер |
|---------|---------------|---------|
| ClickHouse events | 100M rows | ~50GB |
| ClickHouse metrics | 50M rows | ~20GB |
| PostgreSQL sales | 10M rows | ~5GB |
| PostgreSQL users | 1M rows | ~500MB |
| MySQL orders | 500K rows | ~200MB |

### Генерация данных

```bash
# Через docker-compose
docker-compose --profile generate-data up data-generator

# Вручную (Python)
cd tests/load_testing
python -m data.generators.generate_all \
    --clickhouse-rows 100000000 \
    --postgres-rows 10000000 \
    --batch-size 100000
```

## 🐛 Отладка

### Логирование

```bash
# Включить подробные логи
export LOG_LEVEL=DEBUG
export LOG_REQUESTS=true
export LOG_RESPONSES=true

locust -f locustfile.py --host=http://localhost:8088
```

### Проверка подключений

```bash
# ClickHouse
docker-compose exec clickhouse clickhouse-client --query "SELECT count() FROM loadtest.events"

# PostgreSQL
docker-compose exec postgres psql -U loadtest -d loadtest -c "SELECT count(*) FROM sales"

# MySQL
docker-compose exec mysql mysql -u loadtest -ploadtest loadtest -e "SELECT count(*) FROM orders"

# Redis
docker-compose exec redis redis-cli ping
```

## 📝 Примеры сценариев

### Простой тест дашбордов

```python
from locust import HttpUser, task, between

class SimpleDashboardUser(HttpUser):
    wait_time = between(1, 3)
    
    def on_start(self):
        # Login
        self.client.post("/login/", data={
            "username": "admin",
            "password": "admin"
        })
    
    @task
    def view_dashboards(self):
        self.client.get("/api/v1/dashboard/")
```

### Тест SQL Lab

```python
@task
def execute_query(self):
    self.client.post("/api/v1/sqllab/execute/", json={
        "database_id": 1,
        "sql": "SELECT count(*) FROM events",
        "runAsync": False
    })
```

## 🤝 Расширение

### Добавление нового сценария

1. Создайте класс в `scenarios/`:
```python
class MyScenario:
    def __init__(self, client):
        self.client = client
    
    def my_test(self):
        return self.client.get("/api/v1/my-endpoint/")
```

2. Добавьте в `locustfile.py`:
```python
@task
def my_test(self):
    if self.my_scenario:
        self.my_scenario.my_test()
```

### Добавление метрик

```python
from utils.metrics import track_custom_metric

track_custom_metric("my_metric", value, {"tag": "value"})
```

## 📄 Лицензия

Apache License 2.0 — см. [LICENSE](../../../LICENSE.txt)
