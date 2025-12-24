-- PostgreSQL initialization script for load testing
-- Creates tables for transactional/OLTP workloads

-- Enable extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_stat_statements";

-- ============================================
-- Sales data (target: 10M+ rows)
-- ============================================
CREATE TABLE IF NOT EXISTS sales (
    sale_id BIGSERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    amount DECIMAL(12, 2) NOT NULL,
    quantity INTEGER DEFAULT 1,
    discount DECIMAL(5, 2) DEFAULT 0,
    sale_date TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    region_id INTEGER,
    category_id INTEGER,
    payment_method VARCHAR(50),
    status VARCHAR(20) DEFAULT 'completed',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_sales_date ON sales(sale_date);
CREATE INDEX idx_sales_product ON sales(product_id);
CREATE INDEX idx_sales_user ON sales(user_id);
CREATE INDEX idx_sales_region ON sales(region_id);
CREATE INDEX idx_sales_category ON sales(category_id);
CREATE INDEX idx_sales_status ON sales(status);

-- ============================================
-- Users table (target: 1M rows)
-- ============================================
CREATE TABLE IF NOT EXISTS users (
    user_id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) UNIQUE,
    country VARCHAR(100),
    city VARCHAR(100),
    segment VARCHAR(50),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_login TIMESTAMP WITH TIME ZONE,
    is_active BOOLEAN DEFAULT TRUE,
    total_purchases INTEGER DEFAULT 0,
    lifetime_value DECIMAL(12, 2) DEFAULT 0
);

CREATE INDEX idx_users_country ON users(country);
CREATE INDEX idx_users_segment ON users(segment);
CREATE INDEX idx_users_created ON users(created_at);

-- ============================================
-- Products table
-- ============================================
CREATE TABLE IF NOT EXISTS products (
    product_id SERIAL PRIMARY KEY,
    product_name VARCHAR(255) NOT NULL,
    category_id INTEGER,
    subcategory VARCHAR(100),
    price DECIMAL(10, 2) NOT NULL,
    cost DECIMAL(10, 2),
    brand VARCHAR(100),
    supplier VARCHAR(100),
    stock_quantity INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_products_category ON products(category_id);
CREATE INDEX idx_products_brand ON products(brand);

-- ============================================
-- Categories table
-- ============================================
CREATE TABLE IF NOT EXISTS categories (
    category_id SERIAL PRIMARY KEY,
    category_name VARCHAR(100) NOT NULL,
    parent_category_id INTEGER REFERENCES categories(category_id),
    description TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ============================================
-- Regions table
-- ============================================
CREATE TABLE IF NOT EXISTS regions (
    region_id SERIAL PRIMARY KEY,
    region_name VARCHAR(100) NOT NULL,
    country VARCHAR(100) NOT NULL,
    timezone VARCHAR(50),
    currency VARCHAR(10) DEFAULT 'USD'
);

-- ============================================
-- Events table (for event tracking)
-- ============================================
CREATE TABLE IF NOT EXISTS events (
    event_id BIGSERIAL PRIMARY KEY,
    user_id INTEGER,
    event_type VARCHAR(100) NOT NULL,
    event_properties JSONB,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    session_id VARCHAR(100),
    page_url TEXT,
    referrer TEXT,
    user_agent TEXT,
    ip_address INET
);

CREATE INDEX idx_events_user ON events(user_id);
CREATE INDEX idx_events_type ON events(event_type);
CREATE INDEX idx_events_timestamp ON events(timestamp);
CREATE INDEX idx_events_properties ON events USING GIN(event_properties);

-- ============================================
-- Generate sample data
-- ============================================

-- Insert categories
INSERT INTO categories (category_name, description) VALUES
('Electronics', 'Electronic devices and accessories'),
('Clothing', 'Apparel and fashion items'),
('Home & Garden', 'Home improvement and garden supplies'),
('Sports', 'Sports equipment and accessories'),
('Books', 'Books and publications'),
('Food & Beverages', 'Food products and drinks'),
('Health & Beauty', 'Health and beauty products'),
('Automotive', 'Automotive parts and accessories'),
('Toys & Games', 'Toys and games for all ages'),
('Office Supplies', 'Office and business supplies');

-- Insert regions
INSERT INTO regions (region_name, country, timezone, currency) VALUES
('North America', 'US', 'America/New_York', 'USD'),
('Western Europe', 'DE', 'Europe/Berlin', 'EUR'),
('UK & Ireland', 'UK', 'Europe/London', 'GBP'),
('Asia Pacific', 'JP', 'Asia/Tokyo', 'JPY'),
('Latin America', 'BR', 'America/Sao_Paulo', 'BRL'),
('Australia', 'AU', 'Australia/Sydney', 'AUD'),
('Canada', 'CA', 'America/Toronto', 'CAD'),
('India', 'IN', 'Asia/Kolkata', 'INR'),
('France', 'FR', 'Europe/Paris', 'EUR'),
('Mexico', 'MX', 'America/Mexico_City', 'MXN');

-- Generate products (10K)
INSERT INTO products (product_name, category_id, price, cost, brand, stock_quantity)
SELECT 
    'Product ' || i,
    (i % 10) + 1,
    (random() * 1000)::DECIMAL(10,2),
    (random() * 500)::DECIMAL(10,2),
    'Brand ' || (i % 50),
    (random() * 1000)::INTEGER
FROM generate_series(1, 10000) AS i;

-- Generate users (100K initial, can be expanded)
INSERT INTO users (name, email, country, segment, created_at, total_purchases, lifetime_value)
SELECT 
    'User ' || i,
    'user' || i || '@example.com',
    (ARRAY['US', 'UK', 'DE', 'FR', 'JP', 'BR', 'IN', 'CA', 'AU', 'MX'])[1 + (i % 10)],
    (ARRAY['Premium', 'Standard', 'Basic', 'Enterprise', 'Free'])[1 + (i % 5)],
    NOW() - (random() * INTERVAL '730 days'),
    (random() * 100)::INTEGER,
    (random() * 10000)::DECIMAL(12,2)
FROM generate_series(1, 100000) AS i;

-- Generate sales (1M initial, can be expanded)
INSERT INTO sales (product_id, user_id, amount, quantity, sale_date, region_id, category_id, payment_method, status)
SELECT 
    (random() * 9999 + 1)::INTEGER,
    (random() * 99999 + 1)::INTEGER,
    (random() * 500 + 10)::DECIMAL(12,2),
    (random() * 5 + 1)::INTEGER,
    NOW() - (random() * INTERVAL '365 days'),
    (random() * 9 + 1)::INTEGER,
    (random() * 9 + 1)::INTEGER,
    (ARRAY['credit_card', 'debit_card', 'paypal', 'bank_transfer', 'cash'])[1 + (random() * 4)::INTEGER],
    (ARRAY['completed', 'pending', 'refunded', 'cancelled'])[1 + (random() * 3)::INTEGER]
FROM generate_series(1, 1000000) AS i;

-- Generate events (500K initial)
INSERT INTO events (user_id, event_type, event_properties, timestamp, session_id)
SELECT 
    (random() * 99999 + 1)::INTEGER,
    (ARRAY['page_view', 'click', 'purchase', 'signup', 'login', 'search', 'add_to_cart', 'checkout'])[1 + (random() * 7)::INTEGER],
    jsonb_build_object(
        'value', (random() * 100)::INTEGER,
        'source', (ARRAY['organic', 'paid', 'social', 'email', 'direct'])[1 + (random() * 4)::INTEGER],
        'device', (ARRAY['desktop', 'mobile', 'tablet'])[1 + (random() * 2)::INTEGER]
    ),
    NOW() - (random() * INTERVAL '90 days'),
    uuid_generate_v4()::TEXT
FROM generate_series(1, 500000) AS i;

-- Update statistics
ANALYZE sales;
ANALYZE users;
ANALYZE products;
ANALYZE events;

-- Create some useful views for testing
CREATE OR REPLACE VIEW sales_summary AS
SELECT 
    DATE_TRUNC('day', sale_date) AS date,
    r.region_name,
    c.category_name,
    COUNT(*) AS total_sales,
    SUM(amount) AS total_revenue,
    AVG(amount) AS avg_order_value
FROM sales s
LEFT JOIN regions r ON s.region_id = r.region_id
LEFT JOIN categories c ON s.category_id = c.category_id
GROUP BY DATE_TRUNC('day', sale_date), r.region_name, c.category_name;

CREATE OR REPLACE VIEW user_activity AS
SELECT 
    u.user_id,
    u.name,
    u.segment,
    u.country,
    COUNT(DISTINCT s.sale_id) AS purchases,
    COALESCE(SUM(s.amount), 0) AS total_spent,
    MAX(s.sale_date) AS last_purchase
FROM users u
LEFT JOIN sales s ON u.user_id = s.user_id
GROUP BY u.user_id, u.name, u.segment, u.country;
