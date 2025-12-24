-- ClickHouse initialization script for load testing
-- Creates tables optimized for analytical queries

-- Events table - main analytics table (target: 100M+ rows)
CREATE TABLE IF NOT EXISTS loadtest.events
(
    event_id UUID DEFAULT generateUUIDv4(),
    user_id UInt64,
    event_type LowCardinality(String),
    timestamp DateTime64(3),
    properties String,  -- JSON
    session_id String,
    platform LowCardinality(String),
    country LowCardinality(String),
    region LowCardinality(String),
    device_type LowCardinality(String),
    browser LowCardinality(String),
    value Float64 DEFAULT 0,
    duration_ms UInt32 DEFAULT 0,
    is_converted UInt8 DEFAULT 0,
    created_date Date DEFAULT toDate(timestamp)
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(created_date)
ORDER BY (event_type, user_id, timestamp)
SETTINGS index_granularity = 8192;

-- Distributed events table for cluster queries
CREATE TABLE IF NOT EXISTS loadtest.events_distributed AS loadtest.events
ENGINE = Distributed('default', 'loadtest', 'events', rand());

-- Metrics table - time series metrics (target: 50M+ rows)
CREATE TABLE IF NOT EXISTS loadtest.metrics
(
    metric_id UUID DEFAULT generateUUIDv4(),
    metric_name LowCardinality(String),
    timestamp DateTime64(3),
    value Float64,
    tags Map(String, String),
    host LowCardinality(String),
    service LowCardinality(String),
    environment LowCardinality(String),
    created_date Date DEFAULT toDate(timestamp)
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(created_date)
ORDER BY (metric_name, host, timestamp)
SETTINGS index_granularity = 8192;

-- User sessions table
CREATE TABLE IF NOT EXISTS loadtest.user_sessions
(
    session_id String,
    user_id UInt64,
    started_at DateTime64(3),
    ended_at Nullable(DateTime64(3)),
    duration_seconds UInt32 DEFAULT 0,
    page_views UInt16 DEFAULT 0,
    events_count UInt16 DEFAULT 0,
    platform LowCardinality(String),
    country LowCardinality(String),
    is_bounced UInt8 DEFAULT 0,
    is_converted UInt8 DEFAULT 0,
    created_date Date DEFAULT toDate(started_at)
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(created_date)
ORDER BY (user_id, started_at)
SETTINGS index_granularity = 8192;

-- Materialized view for daily aggregates
CREATE MATERIALIZED VIEW IF NOT EXISTS loadtest.events_daily_mv
ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY (date, event_type, country)
AS SELECT
    toDate(timestamp) as date,
    event_type,
    country,
    count() as event_count,
    uniqExact(user_id) as unique_users,
    sum(value) as total_value,
    avg(duration_ms) as avg_duration
FROM loadtest.events
GROUP BY date, event_type, country;

-- Materialized view for hourly metrics
CREATE MATERIALIZED VIEW IF NOT EXISTS loadtest.metrics_hourly_mv
ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(hour)
ORDER BY (hour, metric_name, host)
AS SELECT
    toStartOfHour(timestamp) as hour,
    metric_name,
    host,
    count() as sample_count,
    avg(value) as avg_value,
    min(value) as min_value,
    max(value) as max_value,
    quantile(0.5)(value) as median_value,
    quantile(0.95)(value) as p95_value
FROM loadtest.metrics
GROUP BY hour, metric_name, host;

-- Generate sample data (small initial batch)
INSERT INTO loadtest.events (user_id, event_type, timestamp, platform, country, value, duration_ms)
SELECT
    rand64() % 1000000 as user_id,
    arrayElement(['page_view', 'click', 'purchase', 'signup', 'login', 'search', 'add_to_cart'], (rand() % 7) + 1) as event_type,
    now() - toIntervalSecond(rand() % (86400 * 365)) as timestamp,
    arrayElement(['web', 'ios', 'android'], (rand() % 3) + 1) as platform,
    arrayElement(['US', 'UK', 'DE', 'FR', 'JP', 'BR', 'IN', 'CA', 'AU', 'MX'], (rand() % 10) + 1) as country,
    rand() % 1000 as value,
    rand() % 60000 as duration_ms
FROM numbers(1000000);  -- 1M initial rows

-- Generate metrics data
INSERT INTO loadtest.metrics (metric_name, timestamp, value, host, service, environment)
SELECT
    arrayElement(['cpu_usage', 'memory_usage', 'disk_io', 'network_in', 'network_out', 'request_latency', 'error_rate'], (rand() % 7) + 1) as metric_name,
    now() - toIntervalSecond(rand() % (86400 * 30)) as timestamp,
    rand() % 100 + (rand() % 100) / 100.0 as value,
    concat('host-', toString(rand() % 50)) as host,
    arrayElement(['api', 'web', 'worker', 'database', 'cache'], (rand() % 5) + 1) as service,
    arrayElement(['production', 'staging', 'development'], (rand() % 3) + 1) as environment
FROM numbers(500000);  -- 500K initial rows

-- Generate user sessions
INSERT INTO loadtest.user_sessions (session_id, user_id, started_at, duration_seconds, page_views, events_count, platform, country, is_bounced, is_converted)
SELECT
    generateUUIDv4() as session_id,
    rand64() % 1000000 as user_id,
    now() - toIntervalSecond(rand() % (86400 * 90)) as started_at,
    rand() % 3600 as duration_seconds,
    rand() % 50 as page_views,
    rand() % 100 as events_count,
    arrayElement(['web', 'ios', 'android'], (rand() % 3) + 1) as platform,
    arrayElement(['US', 'UK', 'DE', 'FR', 'JP', 'BR', 'IN', 'CA', 'AU', 'MX'], (rand() % 10) + 1) as country,
    rand() % 2 as is_bounced,
    rand() % 5 = 0 as is_converted  -- ~20% conversion
FROM numbers(100000);  -- 100K initial rows

-- Optimize tables
OPTIMIZE TABLE loadtest.events FINAL;
OPTIMIZE TABLE loadtest.metrics FINAL;
OPTIMIZE TABLE loadtest.user_sessions FINAL;
