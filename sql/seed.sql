-- Fixture for the text-to-SQL MVP.
--
-- Run through text2sql.seed.seed_database(), which enables PRAGMA foreign_keys
-- BEFORE this script starts (the pragma is a no-op inside a transaction) and
-- checks PRAGMA foreign_key_check afterwards. Everything below is ONE transaction.
--
-- internal_note exists only to prove private columns stay private; it is NOT in
-- text2sql.schema.APPROVED_COLUMNS, so neither the model schema nor the executor
-- allowlist may expose it.

BEGIN;

CREATE TABLE products (
    product_id   INTEGER PRIMARY KEY,
    product_name TEXT NOT NULL,
    category     TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE orders (
    order_id         INTEGER PRIMARY KEY,
    order_date       TEXT    NOT NULL,
    product_id       INTEGER NOT NULL REFERENCES products(product_id),
    quantity         INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
    status           TEXT    NOT NULL CHECK (status IN ('paid', 'cancelled')),
    internal_note    TEXT    NOT NULL DEFAULT 'fixture-only'
) WITHOUT ROWID;

INSERT INTO products (product_id, product_name, category) VALUES
    (1, 'Keyboard', 'Hardware'),
    (2, 'Monitor',  'Hardware'),
    (3, 'Notebook', 'Supplies'),
    (4, 'Pen Set',  'Supplies');

INSERT INTO orders (order_id, order_date, product_id, quantity, unit_price_cents, status) VALUES
    (101, '2025-01-01', 1,  2, 2500, 'paid'),
    (102, '2025-01-10', 2,  1, 6000, 'paid'),
    (103, '2025-01-15', 3,  3, 1500, 'paid'),
    (104, '2025-01-20', 1,  1, 2000, 'paid'),
    (105, '2025-01-31', 4,  2, 1000, 'paid'),
    (106, '2025-01-31', 2, 10, 6000, 'cancelled'),
    (107, '2025-02-01', 2,  1, 5500, 'paid'),
    (108, '2024-12-31', 3,  1, 1500, 'paid');

COMMIT;
