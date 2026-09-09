-- Store Director Portal — warehouse schema (SQLite)
-- Grain and provenance are noted per table. All money in USD, all dates ISO (YYYY-MM-DD),
-- business_date follows the POS business day (Toast closeoutHour), not the calendar day.

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS locations (
  location_id        TEXT PRIMARY KEY,           -- slug from config/locations.json
  name               TEXT NOT NULL,
  short              TEXT,
  toast_guid         TEXT,
  marginedge_unit_id TEXT,
  timezone           TEXT DEFAULT 'America/Chicago',
  opened             TEXT,
  state              TEXT
);

-- ---------- Toast (POS) ----------

CREATE TABLE IF NOT EXISTS toast_orders (           -- grain: one order (may hold many checks)
  order_guid        TEXT PRIMARY KEY,
  location_id       TEXT NOT NULL,
  business_date     TEXT NOT NULL,
  opened_at         TEXT, closed_at TEXT, modified_at TEXT,
  dining_option     TEXT,
  revenue_center    TEXT,
  server_guid       TEXT,
  guests            INTEGER DEFAULT 0,
  voided            INTEGER DEFAULT 0,
  checks_count      INTEGER DEFAULT 0,
  net_sales         REAL DEFAULT 0,               -- sum(check.amount): after discounts, before tax
  tax               REAL DEFAULT 0,
  tips              REAL DEFAULT 0,               -- sum(payment.tipAmount)
  discounts         REAL DEFAULT 0,               -- sum(appliedDiscounts.discountAmount) across checks + items
  service_charges   REAL DEFAULT 0,
  gross_sales       REAL DEFAULT 0,               -- net_sales + discounts
  refunds           REAL DEFAULT 0,
  source_hash       TEXT
);
CREATE INDEX IF NOT EXISTS ix_toast_orders_loc_date ON toast_orders(location_id, business_date);

CREATE TABLE IF NOT EXISTS toast_order_items (      -- grain: one selection (menu item line) on a check
  selection_guid    TEXT PRIMARY KEY,
  order_guid        TEXT NOT NULL,
  check_guid        TEXT NOT NULL,
  location_id       TEXT NOT NULL,
  business_date     TEXT NOT NULL,
  item_guid         TEXT,
  item_name         TEXT,
  item_group_guid   TEXT,
  sales_category    TEXT,                          -- Toast sales category name (resolved via menus) or guid
  bucket            TEXT,                          -- Food / Beer / Liquor / Wine / NA Bev / Retail / Other
  quantity          REAL DEFAULT 0,
  pre_discount_price REAL DEFAULT 0,
  price             REAL DEFAULT 0,                -- net of item-level discounts
  tax               REAL DEFAULT 0,
  voided            INTEGER DEFAULT 0,
  hour_local        INTEGER                        -- 0-23, from selection createdDate in location tz
);
CREATE INDEX IF NOT EXISTS ix_toast_items_loc_date ON toast_order_items(location_id, business_date);
CREATE INDEX IF NOT EXISTS ix_toast_items_item ON toast_order_items(item_guid);

CREATE TABLE IF NOT EXISTS toast_payments (         -- grain: one payment (tender) on a check
  payment_guid      TEXT PRIMARY KEY,
  order_guid        TEXT NOT NULL,
  check_guid        TEXT NOT NULL,
  location_id       TEXT NOT NULL,
  business_date     TEXT NOT NULL,
  type              TEXT,                          -- CASH / CREDIT / GIFTCARD / OTHER / HOUSE_ACCOUNT ...
  card_type         TEXT,
  amount            REAL DEFAULT 0,
  tip_amount        REAL DEFAULT 0,
  refund_amount     REAL DEFAULT 0,
  paid_at           TEXT
);
CREATE INDEX IF NOT EXISTS ix_toast_pay_loc_date ON toast_payments(location_id, business_date);

CREATE TABLE IF NOT EXISTS toast_time_entries (     -- grain: one clock-in/clock-out
  entry_guid        TEXT PRIMARY KEY,
  location_id       TEXT NOT NULL,
  business_date     TEXT NOT NULL,
  employee_guid     TEXT,
  job_guid          TEXT,
  job_name          TEXT,
  in_at             TEXT, out_at TEXT,
  regular_hours     REAL DEFAULT 0,
  overtime_hours    REAL DEFAULT 0,
  hourly_wage       REAL DEFAULT 0,
  wages             REAL DEFAULT 0,                -- regular*wage + overtime*wage*1.5
  declared_cash_tips REAL DEFAULT 0,
  non_cash_tips     REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_toast_te_loc_date ON toast_time_entries(location_id, business_date);

CREATE TABLE IF NOT EXISTS toast_menu_items (       -- grain: menu item per location (latest published menu)
  item_guid         TEXT NOT NULL,
  location_id       TEXT NOT NULL,
  name              TEXT,
  menu_group        TEXT,
  sales_category    TEXT,
  price             REAL,
  PRIMARY KEY (item_guid, location_id)
);

CREATE TABLE IF NOT EXISTS toast_jobs (
  job_guid TEXT NOT NULL, location_id TEXT NOT NULL, title TEXT, wage_frequency TEXT, default_wage REAL,
  PRIMARY KEY (job_guid, location_id)
);

-- ---------- MarginEdge (purchasing / COGS / inventory) ----------

CREATE TABLE IF NOT EXISTS me_categories (
  category_id     TEXT NOT NULL, location_id TEXT NOT NULL,
  name TEXT, category_type TEXT, accounting_code TEXT, bucket TEXT,
  PRIMARY KEY (category_id, location_id)
);

CREATE TABLE IF NOT EXISTS me_vendors (
  vendor_id TEXT NOT NULL, location_id TEXT NOT NULL, name TEXT, central_vendor_id TEXT,
  PRIMARY KEY (vendor_id, location_id)
);

CREATE TABLE IF NOT EXISTS me_products (
  product_id TEXT NOT NULL, location_id TEXT NOT NULL,
  name TEXT, central_product_id TEXT, latest_price REAL, report_unit TEXT, tax_exempt INTEGER,
  categories_json TEXT,                              -- [{categoryId, percentAllocation}]
  primary_category_id TEXT,
  PRIMARY KEY (product_id, location_id)
);

CREATE TABLE IF NOT EXISTS me_invoices (               -- grain: one order/invoice (or credit) in MarginEdge
  order_id        TEXT NOT NULL, location_id TEXT NOT NULL,
  vendor_id TEXT, vendor_name TEXT, invoice_number TEXT,
  invoice_date TEXT, created_date TEXT,
  order_total REAL DEFAULT 0, tax REAL DEFAULT 0, delivery_charges REAL DEFAULT 0, other_charges REAL DEFAULT 0,
  credit_amount REAL DEFAULT 0, is_credit INTEGER DEFAULT 0,
  status TEXT, payment_account TEXT,
  PRIMARY KEY (order_id, location_id)
);
CREATE INDEX IF NOT EXISTS ix_me_inv_loc_date ON me_invoices(location_id, invoice_date);

CREATE TABLE IF NOT EXISTS me_invoice_lines (          -- grain: one line item on an invoice
  order_id TEXT NOT NULL, location_id TEXT NOT NULL, line_no INTEGER NOT NULL,
  invoice_date TEXT,
  vendor_item_code TEXT, vendor_item_name TEXT,
  product_id TEXT, category_id TEXT, packaging_id TEXT, bucket TEXT,
  quantity REAL DEFAULT 0, unit_price REAL DEFAULT 0, line_price REAL DEFAULT 0,
  PRIMARY KEY (order_id, location_id, line_no)
);
CREATE INDEX IF NOT EXISTS ix_me_lines_loc_date ON me_invoice_lines(location_id, invoice_date);

CREATE TABLE IF NOT EXISTS me_inventory_counts (       -- grain: one bucket value per count date per location
  location_id TEXT NOT NULL, count_date TEXT NOT NULL, bucket TEXT NOT NULL,
  value REAL DEFAULT 0, source TEXT DEFAULT 'csv',      -- 'api' once the count-sheet endpoint is wired, else inputs/inventory_counts.csv
  PRIMARY KEY (location_id, count_date, bucket)
);

-- ---------- Manual inputs (inputs/*.csv) ----------

CREATE TABLE IF NOT EXISTS activations (               -- market activations / events / promos
  activation_id TEXT PRIMARY KEY,
  location_id TEXT NOT NULL,
  start_date TEXT NOT NULL, end_date TEXT,
  name TEXT, type TEXT, cost REAL DEFAULT 0, owner TEXT, notes TEXT
);

CREATE TABLE IF NOT EXISTS targets (                   -- monthly budget/targets per location
  location_id TEXT NOT NULL, month TEXT NOT NULL,      -- month = YYYY-MM
  sales_target REAL, cogs_pct_target REAL, labor_pct_target REAL, guests_target INTEGER,
  PRIMARY KEY (location_id, month)
);

-- ---------- Derived ----------

CREATE TABLE IF NOT EXISTS daily_summary (             -- grain: location × business date; rebuilt by transform
  location_id TEXT NOT NULL, business_date TEXT NOT NULL,
  net_sales REAL DEFAULT 0, gross_sales REAL DEFAULT 0, discounts REAL DEFAULT 0, tax REAL DEFAULT 0,
  tips REAL DEFAULT 0, refunds REAL DEFAULT 0,
  orders INTEGER DEFAULT 0, checks INTEGER DEFAULT 0, guests INTEGER DEFAULT 0,
  sales_food REAL DEFAULT 0, sales_beer REAL DEFAULT 0, sales_liquor REAL DEFAULT 0, sales_wine REAL DEFAULT 0,
  sales_nabev REAL DEFAULT 0, sales_retail REAL DEFAULT 0, sales_other REAL DEFAULT 0,
  labor_hours REAL DEFAULT 0, labor_cost REAL DEFAULT 0,
  purchases REAL DEFAULT 0, purch_food REAL DEFAULT 0, purch_beer REAL DEFAULT 0, purch_liquor REAL DEFAULT 0,
  purch_wine REAL DEFAULT 0, purch_nabev REAL DEFAULT 0, purch_other REAL DEFAULT 0,
  PRIMARY KEY (location_id, business_date)
);

CREATE TABLE IF NOT EXISTS pull_log (                  -- what was pulled when (drives incremental windows)
  source TEXT NOT NULL, location_id TEXT NOT NULL, dataset TEXT NOT NULL,
  window_start TEXT, window_end TEXT, pulled_at TEXT, rows INTEGER, ok INTEGER, note TEXT
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
