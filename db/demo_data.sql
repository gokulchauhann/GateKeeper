-- ---------------------------------------------------------------------
-- Demo business data: each tenant's own customers and orders.
-- Run AFTER db/schema.sql. Every row carries tenant_id, so a tenant only
-- ever sees its own data, and the orders table is large enough (filled by
-- db/seed.py) that the heavy report really loads the CPU.
-- ---------------------------------------------------------------------
CREATE TABLE customers (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    name VARCHAR(100) NOT NULL,
    email VARCHAR(150) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'active',   -- 'active' or 'inactive'
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    amount NUMERIC(10, 2) NOT NULL,
    status VARCHAR(20) NOT NULL,   -- pending / paid / shipped / delivered / refunded
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Normal pages filter by tenant, so these keep them fast.
CREATE INDEX idx_customers_tenant ON customers(tenant_id);
CREATE INDEX idx_orders_tenant_created ON orders(tenant_id, created_at);
