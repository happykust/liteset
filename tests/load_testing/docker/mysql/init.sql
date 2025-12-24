-- MySQL initialization script for load testing
-- Additional database for testing multiple datasources

-- ============================================
-- Orders table
-- ============================================
CREATE TABLE IF NOT EXISTS orders (
    order_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    customer_id INT NOT NULL,
    order_date DATETIME DEFAULT CURRENT_TIMESTAMP,
    total_amount DECIMAL(12, 2) NOT NULL,
    discount_amount DECIMAL(10, 2) DEFAULT 0,
    shipping_cost DECIMAL(10, 2) DEFAULT 0,
    status VARCHAR(50) DEFAULT 'pending',
    payment_method VARCHAR(50),
    shipping_address TEXT,
    billing_address TEXT,
    notes TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_customer (customer_id),
    INDEX idx_date (order_date),
    INDEX idx_status (status)
) ENGINE=InnoDB;

-- ============================================
-- Order items table
-- ============================================
CREATE TABLE IF NOT EXISTS order_items (
    item_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_id BIGINT NOT NULL,
    product_id INT NOT NULL,
    quantity INT DEFAULT 1,
    unit_price DECIMAL(10, 2) NOT NULL,
    discount_percent DECIMAL(5, 2) DEFAULT 0,
    total_price DECIMAL(12, 2) NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(order_id),
    INDEX idx_order (order_id),
    INDEX idx_product (product_id)
) ENGINE=InnoDB;

-- ============================================
-- Customers table
-- ============================================
CREATE TABLE IF NOT EXISTS customers (
    customer_id INT AUTO_INCREMENT PRIMARY KEY,
    first_name VARCHAR(100) NOT NULL,
    last_name VARCHAR(100) NOT NULL,
    email VARCHAR(255) UNIQUE,
    phone VARCHAR(50),
    customer_segment VARCHAR(50),
    registration_date DATE,
    last_order_date DATE,
    total_orders INT DEFAULT 0,
    total_spent DECIMAL(12, 2) DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_segment (customer_segment),
    INDEX idx_registration (registration_date)
) ENGINE=InnoDB;

-- ============================================
-- Inventory table
-- ============================================
CREATE TABLE IF NOT EXISTS inventory (
    inventory_id INT AUTO_INCREMENT PRIMARY KEY,
    product_id INT NOT NULL,
    warehouse_id INT NOT NULL,
    quantity INT DEFAULT 0,
    reserved_quantity INT DEFAULT 0,
    reorder_level INT DEFAULT 10,
    last_restocked DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_product_warehouse (product_id, warehouse_id),
    INDEX idx_product (product_id),
    INDEX idx_warehouse (warehouse_id)
) ENGINE=InnoDB;

-- ============================================
-- Warehouses table
-- ============================================
CREATE TABLE IF NOT EXISTS warehouses (
    warehouse_id INT AUTO_INCREMENT PRIMARY KEY,
    warehouse_name VARCHAR(100) NOT NULL,
    location VARCHAR(255),
    country VARCHAR(100),
    capacity INT,
    is_active BOOLEAN DEFAULT TRUE
) ENGINE=InnoDB;

-- ============================================
-- Generate sample data
-- ============================================

-- Warehouses
INSERT INTO warehouses (warehouse_name, location, country, capacity) VALUES
('Main Warehouse', 'New York, NY', 'US', 100000),
('West Coast Hub', 'Los Angeles, CA', 'US', 75000),
('European Center', 'Frankfurt', 'DE', 50000),
('UK Distribution', 'London', 'UK', 40000),
('Asia Pacific', 'Singapore', 'SG', 60000);

-- Customers (100K)
DELIMITER //
CREATE PROCEDURE generate_customers()
BEGIN
    DECLARE i INT DEFAULT 1;
    WHILE i <= 100000 DO
        INSERT INTO customers (first_name, last_name, email, customer_segment, registration_date, total_orders, total_spent)
        VALUES (
            CONCAT('First', i),
            CONCAT('Last', i),
            CONCAT('customer', i, '@example.com'),
            ELT(1 + FLOOR(RAND() * 4), 'Premium', 'Standard', 'Basic', 'VIP'),
            DATE_SUB(CURDATE(), INTERVAL FLOOR(RAND() * 730) DAY),
            FLOOR(RAND() * 50),
            ROUND(RAND() * 5000, 2)
        );
        SET i = i + 1;
    END WHILE;
END //
DELIMITER ;

CALL generate_customers();
DROP PROCEDURE generate_customers;

-- Orders (500K)
DELIMITER //
CREATE PROCEDURE generate_orders()
BEGIN
    DECLARE i INT DEFAULT 1;
    WHILE i <= 500000 DO
        INSERT INTO orders (customer_id, order_date, total_amount, status, payment_method)
        VALUES (
            1 + FLOOR(RAND() * 100000),
            DATE_SUB(NOW(), INTERVAL FLOOR(RAND() * 365) DAY),
            ROUND(10 + RAND() * 500, 2),
            ELT(1 + FLOOR(RAND() * 4), 'completed', 'pending', 'shipped', 'cancelled'),
            ELT(1 + FLOOR(RAND() * 4), 'credit_card', 'paypal', 'bank_transfer', 'cash')
        );
        SET i = i + 1;
    END WHILE;
END //
DELIMITER ;

CALL generate_orders();
DROP PROCEDURE generate_orders;

-- Order Items (1M)
DELIMITER //
CREATE PROCEDURE generate_order_items()
BEGIN
    DECLARE i INT DEFAULT 1;
    DECLARE qty INT;
    DECLARE price DECIMAL(10,2);
    WHILE i <= 1000000 DO
        SET qty = 1 + FLOOR(RAND() * 5);
        SET price = ROUND(5 + RAND() * 200, 2);
        INSERT INTO order_items (order_id, product_id, quantity, unit_price, total_price)
        VALUES (
            1 + FLOOR(RAND() * 500000),
            1 + FLOOR(RAND() * 10000),
            qty,
            price,
            qty * price
        );
        SET i = i + 1;
    END WHILE;
END //
DELIMITER ;

CALL generate_order_items();
DROP PROCEDURE generate_order_items;

-- Inventory
INSERT INTO inventory (product_id, warehouse_id, quantity, reserved_quantity, reorder_level)
SELECT 
    p.product_id,
    w.warehouse_id,
    FLOOR(RAND() * 1000),
    FLOOR(RAND() * 100),
    FLOOR(10 + RAND() * 50)
FROM 
    (SELECT @rownum := @rownum + 1 AS product_id FROM 
        (SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5) t1,
        (SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5) t2,
        (SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5) t3,
        (SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5) t4,
        (SELECT @rownum := 0) r
    ) p
CROSS JOIN warehouses w
LIMIT 50000;

-- Create useful views
CREATE OR REPLACE VIEW order_summary AS
SELECT 
    DATE(order_date) AS order_date,
    status,
    payment_method,
    COUNT(*) AS order_count,
    SUM(total_amount) AS total_revenue,
    AVG(total_amount) AS avg_order_value
FROM orders
GROUP BY DATE(order_date), status, payment_method;

CREATE OR REPLACE VIEW customer_orders AS
SELECT 
    c.customer_id,
    CONCAT(c.first_name, ' ', c.last_name) AS customer_name,
    c.customer_segment,
    COUNT(o.order_id) AS order_count,
    COALESCE(SUM(o.total_amount), 0) AS total_spent,
    MAX(o.order_date) AS last_order_date
FROM customers c
LEFT JOIN orders o ON c.customer_id = o.customer_id
GROUP BY c.customer_id, c.first_name, c.last_name, c.customer_segment;
