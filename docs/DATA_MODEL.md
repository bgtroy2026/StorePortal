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
| MarginEdge inventories (when wired) or `inputs/inventory_counts.csv` | `me_inventory_counts` | location × count date × bucket | |
| `inputs/activations.csv` | `activations` | activation | date range, type, cost, owner |
| `inputs/targets.csv` | `targets` | location × month | sales, COGS %, labor %, guests |
| derived | `daily_summary` | location × business date | rebuilt every transform |

## Buckets

Food · Beer · Liquor · Wine · NA Bev · Retail · Other. Both Toast sales categories and MarginEdge category
types map onto the same seven buckets (`config/settings.json → category_map`) so sales and purchases line up
for cost %.

## KPI definitions (dashboard)

| KPI | Definition |
|---|---|
| Net sales | Σ check amounts after discounts, before tax; voided excluded |
| Guests / Avg check / PPA | `numberOfGuests` on orders; net ÷ checks; net ÷ guests |
| Labor % | labor cost ÷ net sales (time entries: reg + OT×1.5 at hourly wage; tipped wages as recorded in Toast) |
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
- MarginEdge: list orders for the window; fetch detail only for orders without lines yet or still non-final.
- Transform is idempotent (replace-by-day for Toast, upsert by id elsewhere), so re-running is always safe.
- The warehouse is gzip+AES-GCM encrypted and stored as a release asset (`warehouse-state`) between runs.
