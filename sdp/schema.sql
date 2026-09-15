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

-- Guid -> name lookups from config/v2. Orders, checks and selections reference these by guid only, so a
-- report without them reads as rows of hex. Kept as one generic table rather than one per resource: they all
-- have the same shape, and a view that needs a new one should not need a new migration.
CREATE TABLE IF NOT EXISTS toast_config (
  location_id TEXT NOT NULL, resource TEXT NOT NULL, guid TEXT NOT NULL,
  name TEXT, extra TEXT,
  PRIMARY KEY (location_id, resource, guid)
);

CREATE TABLE IF NOT EXISTS toast_employees (      -- grain: one employee per location
  employee_guid TEXT NOT NULL, location_id TEXT NOT NULL,
  first_name TEXT, last_name TEXT, chosen_name TEXT, display_name TEXT,
  email TEXT, external_id TEXT,
  deleted INTEGER DEFAULT 0, disabled INTEGER DEFAULT 0,
  job_guids TEXT,                                 -- JSON array of job guids from jobReferences
  PRIMARY KEY (employee_guid, location_id)
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

CREATE TABLE IF NOT EXISTS me_sales_daily (             -- grain: location × business date × ME sales category (GET /sales/report per day)
  location_id TEXT NOT NULL, business_date TEXT NOT NULL, category_id TEXT NOT NULL,
  category_name TEXT, bucket TEXT, total REAL DEFAULT 0,
  PRIMARY KEY (location_id, business_date, category_id)
);

CREATE TABLE IF NOT EXISTS me_pnl_daily (               -- grain: location × business date × P&L line (GET /profitAndLoss/report per day)
  location_id TEXT NOT NULL, business_date TEXT NOT NULL,
  section TEXT NOT NULL,                                -- income | cogs | labor | expenses
  category_id TEXT, category_name TEXT, item_name TEXT, -- item_name NULL = category total row; category NULL = section total
  total REAL DEFAULT 0, pct_of_sales REAL, bucket TEXT,
  PRIMARY KEY (location_id, business_date, section, category_id, item_name)
);
CREATE INDEX IF NOT EXISTS ix_me_pnl_loc_date ON me_pnl_daily(location_id, business_date);

CREATE TABLE IF NOT EXISTS me_pnl_summary (             -- grain: location × business date (P&L summary block)
  location_id TEXT NOT NULL, business_date TEXT NOT NULL,
  income_total REAL, cogs_total REAL, labor_total REAL, expenses_total REAL,
  gross_profit REAL, prime_cost REAL, controllable_profit REAL,
  PRIMARY KEY (location_id, business_date)
);

CREATE TABLE IF NOT EXISTS me_inventories (             -- grain: one inventory (count event)
  inventory_id TEXT NOT NULL, location_id TEXT NOT NULL,
  countsheet_id TEXT, countsheet_name TEXT, inventory_date TEXT, status TEXT, total_value REAL,
  closed_date TEXT, saved_date TEXT, origin TEXT,
  PRIMARY KEY (inventory_id, location_id)
);

CREATE TABLE IF NOT EXISTS me_inventory_items (         -- grain: one counted product line in an inventory
  inventory_id TEXT NOT NULL, location_id TEXT NOT NULL, item_id TEXT NOT NULL,
  section_name TEXT, product_id TEXT, product_name TEXT, central_product_id TEXT,
  quantity REAL, price REAL, value REAL, unit TEXT, unit_size REAL, bucket TEXT,
  PRIMARY KEY (inventory_id, location_id, item_id)
);

CREATE TABLE IF NOT EXISTS me_inventory_counts (       -- grain: one bucket value per count date per location (derived from items, or CSV fallback)
  location_id TEXT NOT NULL, count_date TEXT NOT NULL, bucket TEXT NOT NULL,
  value REAL DEFAULT 0, source TEXT DEFAULT 'csv',      -- 'api' = rolled up from me_inventory_items; 'csv' = inputs/inventory_counts.csv
  PRIMARY KEY (location_id, count_date, bucket)
);

-- ---------- Tripleseat (private events / banquets) ----------

CREATE TABLE IF NOT EXISTS ts_events (                  -- grain: one Tripleseat event
  event_id TEXT NOT NULL, location_id TEXT NOT NULL,
  ts_location_id TEXT, booking_id TEXT, name TEXT, status TEXT, event_type TEXT, event_style TEXT,
  event_date TEXT, start_at TEXT, end_at TEXT,
  guest_count INTEGER, guaranteed_guest_count INTEGER,
  fb_minimum REAL, rental_fee REAL, deposit REAL,
  grand_total REAL,                                     -- booked/contracted value
  actual_amount REAL,                                   -- what was actually billed (after the event)
  amount_due REAL, price_per_person REAL,
  created_at TEXT, updated_at TEXT,
  PRIMARY KEY (event_id, location_id)
);
CREATE INDEX IF NOT EXISTS ix_ts_events_loc_date ON ts_events(location_id, event_date);

CREATE TABLE IF NOT EXISTS ts_leads (                   -- grain: one inbound lead (the pipeline behind events)
  lead_id TEXT NOT NULL, location_id TEXT NOT NULL,
  ts_location_id TEXT, company TEXT, contact_name TEXT, status TEXT, source TEXT,
  event_date TEXT, guest_count INTEGER, description TEXT, created_at TEXT, updated_at TEXT,
  PRIMARY KEY (lead_id, location_id)
);
CREATE INDEX IF NOT EXISTS ix_ts_leads_loc_date ON ts_leads(location_id, event_date);

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

-- Leadership Scorecard (Google Sheets, read as-is: see sdp/scorecard.py for why it is not recomputed).
-- One row per metric per week. `value` is the raw number when the cell held one, `display` the sheet's own
-- formatting, which is what the page shows so a figure reads exactly as leadership is used to seeing it.
CREATE TABLE IF NOT EXISTS scorecard (
  metric      TEXT NOT NULL,
  week        TEXT NOT NULL,
  owner       TEXT,
  value       REAL,
  display     TEXT,
  seq         INTEGER,          -- row order in the sheet, so the page can preserve its grouping
  PRIMARY KEY (metric, week)
);
CREATE TABLE IF NOT EXISTS scorecard_goals (
  metric      TEXT PRIMARY KEY,
  owner       TEXT,
  value       REAL,
  display     TEXT,
  seq         INTEGER
);

-- Every visible tab of the scorecard workbook, stored as the sheet's own display grid (JSON 2-D array).
-- Grids rather than parsed columns because the tabs have no common shape: some are weekly metrics, some are
-- per-location, some are distributor extracts. The portal renders them as the sheet formats them.
CREATE TABLE IF NOT EXISTS scorecard_tabs (
  tab       TEXT PRIMARY KEY,
  seq       INTEGER,            -- workbook order, so the portal's tab strip matches the sheet
  rows      INTEGER,            -- true size in the sheet, which may exceed what was fetched
  cols      INTEGER,
  truncated INTEGER,            -- 1 when the tab was capped, so the portal can say so rather than imply completeness
  grid      TEXT
);

-- Every discount applied to a check or an item, kept individually rather than summed.
-- The aggregate `discounts` column answers "how much did we give away"; this answers "to whom, and why" --
-- separating loyalty redemptions from manager comps, employee meals and promotional pricing. Toast names the
-- loyalty provider in appliedDiscounts.loyaltyDetails, which is how Thanx redemptions are identified without
-- integrating Thanx at all.
CREATE TABLE IF NOT EXISTS toast_discounts (
  discount_guid   TEXT,                -- appliedDiscount guid; unique per check/selection application
  order_guid      TEXT,
  check_guid      TEXT,
  location_id     TEXT NOT NULL,
  business_date   TEXT NOT NULL,
  name            TEXT,                -- as the restaurant named it in Toast
  discount_type   TEXT,                -- PERCENT / FIXED / OPEN etc.
  scope           TEXT,                -- 'check' or 'item'
  loyalty_vendor  TEXT,                -- from loyaltyDetails.vendor, e.g. the loyalty provider
  amount          REAL DEFAULT 0,
  PRIMARY KEY (discount_guid, scope)
);
CREATE INDEX IF NOT EXISTS ix_toast_disc_day ON toast_discounts (location_id, business_date);
