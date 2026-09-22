# Data model

Warehouse: SQLite (`state/warehouse.sqlite`), schema in `sdp/schema.sql`. Money in USD; dates ISO;
`business_date` is the POS business day (Toast `closeoutHour`, 4 AM), not the calendar day.

## Sources → tables

| Source endpoint | Table | Grain | Notes |
|---|---|---|---|
| Toast `orders/v2/ordersBulk?businessDate` | `toast_orders` | order | net = Σ check.amount (post-discount, pre-tax); gross = net + discounts; voided orders kept with 0 sales |
| ↳ checks[].selections[] | `toast_order_items` | menu item line | `bucket` from sales category via `settings.category_map`; `hour_local` for the heatmap |
| ↳ checks[].payments[] | `toast_payments` | tender | tips, refunds, card type |
| Toast `labor/v1/timeEntries` | `toast_time_entries` | clock-in/out | wages = reg×rate + OT×rate×1.5; tips declared/non-cash |
| Toast `labor/v1/jobs`, `menus/v2/menus` | `toast_jobs`, `toast_menu_items` | reference | job titles; item → sales category |
| MarginEdge `/orders` + `/orders/{id}` | `me_invoices`, `me_invoice_lines` | invoice / line | credits negative; header-only invoices still count toward purchases |
| MarginEdge `/categories`, `/vendors`, `/products` | `me_categories`, `me_vendors`, `me_products` | reference | category type → bucket |
| MarginEdge `/sales/report` (per day) | `me_sales_daily` | location × business date × ME sales category | Toast sales as MarginEdge receives them; the sales source when Toast is not connected |
| MarginEdge `/profitAndLoss/report` (per day) | `me_pnl_daily`, `me_pnl_summary` | location × business date × section/category/item | income, cogs, labor, expenses; labor source when Toast is not connected |
| MarginEdge `/inventories` + detail + section items | `me_inventories`, `me_inventory_items` | inventory; counted product | product → primary category → bucket |
| derived (or `inputs/inventory_counts.csv` fallback) | `me_inventory_counts` | location × count date × bucket | `source` = api / csv; API wins |
| Tripleseat public key `/locations`, `/sites`, `/lead_forms` | `ts_rooms`, `ts_catalog` | room; picklist entry / billing rule | replaced whole each night; `ts_catalog.kind` ∈ location, event_type, lead_source, referral_source, line_item_category, billing (per taproom, `value` = rate), lead_form |
| Tripleseat webhooks via the Apps Script "Tripleseat" tab — live deliveries plus the rows `tools/tripleseat_seed.js` posted from an Event Details report export (or `/events/search`, `/leads/search` when the OAuth API exists) | `ts_events`, `ts_leads` | event; lead | `source`/`origin` = api, webhook or seed (a seeded row keeps `seed` until a live delivery replaces it). `seen_at` is when the state was true — the delivery time, or the report's export time for a seeded row — and the newest state per object wins whatever the order on the tab; a `backfill` run re-reads the whole tab and rebuilds these rows from it. A DELETE sets `deleted=1` rather than removing the row so a late re-send cannot resurrect it. `rooms` and `event_type_name` are resolved from the catalog. A taproom that is a *room* of another location (Solon in Iowa City) is matched by `tripleseat_room_ids` before `tripleseat_location_id`. |
| `inputs/activations.csv` | `activations` | activation | date range, type, cost, owner |
| `inputs/targets.csv` | `targets` | location × month | sales, COGS %, labor %, guests |
| derived | `daily_summary` | location × business date | rebuilt every transform |

## Buckets

Food · Beer · Liquor · Wine · NA Bev · Retail · Other. Toast sales categories, MarginEdge sales-report
categories and MarginEdge purchasing category types all map onto the same seven buckets
(`config/settings.json → category_map`) so sales, purchases and inventory line up for cost %.

## Source precedence (daily_summary)

Per location × day: net sales and category sales come from Toast orders when that day was pulled from Toast,
otherwise from `me_sales_daily`; labor cost from Toast time entries, otherwise `me_pnl_summary.labor_total`;
purchases always from MarginEdge invoices. `payload.sources[location]` tells the dashboard which applies.

## KPI definitions (dashboard)

| KPI | Definition |
|---|---|
| Net sales | Σ check amounts after discounts, before tax; voided excluded |
| Guests / Avg check / PPA | `numberOfGuests` on orders; net ÷ checks; net ÷ guests |
| Labor % | labor cost ÷ net sales — Toast time entries (reg + OT×1.5 at hourly wage) when Toast is connected, else the P&L labor total for the day |
| SPLH | net sales ÷ labor hours |
| Purchases % (COGS proxy) | invoice totals (by invoice date) ÷ net sales — a purchasing-based proxy |
| Category cost % | purchases in bucket ÷ net sales in bucket |
| Actual cost % (Inventory page) | (begin count + purchases − end count) ÷ sales for the count interval |
| Weeks on hand | end count ÷ (usage per day × 7) |
| Activation lift | net sales on activation days − average of same weekdays in the prior 4 weeks |
| vs prior / vs LY | same-length window immediately before / same dates one year earlier (needs ≥ 13 months of history) |

## Incremental behaviour

- Toast: every night re-pull the last `incremental_days` (7) business days per location (tips/voids/refunds
  settle after close) plus any day inside `backfill_days` that the warehouse doesn't have.
- MarginEdge: list orders for the window; fetch detail only for orders without lines yet or not yet CLOSED. Daily sales report and P&L are re-pulled for the last 7 days and fetched for any older day missing from the warehouse; inventories are re-fetched when their savedDate changes. 1 request/second; long backfills stop at `max_minutes_per_run` and resume next run.
- Transform is idempotent (replace-by-day for Toast, upsert by id elsewhere), so re-running is always safe.
- The warehouse is gzip+AES-GCM encrypted and stored as a release asset (`warehouse-state`) between runs.

## Added 2026-09-19

| Table / column | Grain | Notes |
|---|---|---|
| `toast_order_items.modifiers`, `size_oz`, `pour` | selection | Modifier text verbatim; ounces per unit and draft/package as read by `sdp/pours.py`. `modifiers IS NULL` means the day predates capture — never "no modifier". Sizes are re-derived from the stored text on every transform, so a config fix reaches history. |
| `toast_orders.source` | order | Toast order source. |
| `toast_loyalty` | check | A check on which a loyalty member identified themselves. `member` is a truncated SHA-256; the identifier itself is never stored. |
| `weather_daily` | location × date | Open-Meteo daily high/low/precipitation/WMO code. `kind` = observed or forecast. |
| `drawer_floats` | location × drawer | From `inputs/floats.csv`. |
| `depletions` | location × brand × premise | From `inputs/depletions.csv`. Case equivalents YTD and same span last year. |

Payload sections added: `beer`, `channels`, `loyalty`, `menu`, `beer_mix`, `compliance`, `weather`, `market`; `cash[*].float_check`;
each digest row gained a fifth element, a stable rule key, which acknowledgements are filed against. The detail bundle gained
`range` — daily-grain tables behind the date picker. `data/<id>.bin` for label `digest` is the morning email payload.

## Operational views — added 2026-09-20 (`sdp/ops.py`)

Payload key `ops[location]`, each sub-section isolated (a failure publishes that one as `null`):

| Section | Built from | Notes |
|---|---|---|
| `comps` | `toast_discounts` (+ `approver_guid`, `reason`, `captured`), `toast_order_items.void_reason`, `toast_orders` | Loyalty redemptions excluded. Discounts whose name contains a word in `comps.promotions` are shown but kept out of the rate and the flag. Flag = rate ≥ 2× the taproom's own AND ≥ $100, only for people with ≥ 40 orders. Approver/void reason exist only on days pulled since 2026-09-20; `approver_coverage` says how much of the window that is, and void reasons on other days read "day not re-pulled yet". `pull --heal` re-pulls the last 60 days for these. |
| `tabs` | `toast_orders.opened_at/closed_at` | On-premise orders only. Median and 75th percentile minutes by part of day; average tabs open at the half-hour of each local hour; tabs open 8h+ counted separately. |
| `overtime` | `toast_time_entries`, `toast_shifts` (now pulled 8 days ahead) | Pay week from `labor.week_start` (0 = Monday), threshold `labor.overtime_hours`. Per taproom. Deleted or no-longer-returned shifts are marked `deleted=1` at load. |
| `checks` | items, payments, orders | Gift cards sold (by name) vs redeemed (tender); items rung at $0; share of orders with a party size of 2+ (Toast defaults to 1, so "1" is mostly silence). |
| `stock` | `toast_stock`, `toast_stock_days` | One snapshot per morning from `/stock/v1/inventory`. Optional scope: without it the panel does not appear. |
| `invoice_health` | `me_invoices` | Entry lag (created − invoice date) and regular vendors gone quiet; the quiet test allows for the taproom's own slowest-tenth entry lag. |
| `price_alerts` | `_price_tracking` movers | Rises that have cost ≥ $150; feeds the digest. |

Company-wide: `menu_prices` (same Toast menu item, different price, ≥ 50¢) and `price_compare` (same vendor + vendor item code, volume-weighted unit price by taproom over 90 days; gaps over 60% are dropped as pack mismatches). In a single-taproom bundle both are cut to that taproom's rows, other taprooms are reduced to "lowest/highest elsewhere", and the gap and its cost are restated for that taproom.

New digest rules: `price:<product>`, `overtime_week` (a count, never names), `quiet:<vendor>`, `comps_outlier`.
