# Methodology

## 1. Data cleaning
- Removed cancelled invoices (invoice numbers starting with `C`).
- Removed rows with non-positive quantity or price.
- Removed non-product codes (e.g. `POST`, `DOT`, `M`, `BANK CHARGES`); valid product codes are 5 digits with an optional letter suffix.

## 2. Warehouse interpretation of the data
- **Order (invoice)** = one picking task.
- **Order line** = one visit to a storage location.
- **Pick frequency** of a SKU = number of order lines containing it.

## 3. ABC classification
- Cumulative share thresholds: A ≤ 80%, B ≤ 95%, C = rest.
- Computed twice (by pick frequency and by revenue) to show that the two classifications differ.

## 4. Synthetic warehouse layout (Module 2)
- Single block with parallel aisles, one front and one back cross aisle. 40 aisles, each with a left and a right rack of 50 slots (1 m per slot): 4,000 slots for 3,791 SKUs, 209 spare.
- Aisle length 50 m; aisle centre lines 3 m apart. Aisle *j* (0-based) is at x = 1.5 + 3*j* m; slot *k* (0-based) is at y = *k* + 0.5 m from the front cross aisle.
- The depot (pick-up and drop-off point) is at the left end of the front cross aisle, x = 0, y = 0.
- **Slot walking distance** = x + y (rectilinear, via the front cross aisle and the aisle). Slots are ranked by this distance; ties are broken by aisle, position, side. Cross-aisle width and the lateral movement between the two racks of an aisle are ignored.

## 5. S-shape routing
- Input per order: the set of distinct SKUs (one visit per SKU, even if the invoice lists it on several lines) mapped to slots.
- Every aisle that contains a pick is traversed end to end. The picker moves between aisles along the front and back cross aisles alternately.
- If the number of aisles with picks is **even**, the route ends at the front cross aisle and returns to the depot. If it is **odd**, the last (rightmost) aisle is entered from the front and left again from its farthest pick.
- Closed form used in the code: with *n* aisles containing picks, *x*<sub>max</sub> the x of the rightmost of them and *y*<sub>far</sub> the farthest pick in that aisle,
  `distance = 2 * x_max + L * (n - n mod 2) + 2 * y_far * (n mod 2)`, where *L* is the aisle length. The horizontal part is always 2 * x_max because the picker goes out to the rightmost aisle and back.
- The formula is verified in `tests/test_routing.py` against three hand-calculated routes (1, 2 and 3 aisles: 42.0 m, 115.0 m, 176.0 m) and against an independent waypoint-by-waypoint simulation on random orders.

## 6. Slotting scenarios
- **Random (baseline):** SKUs are placed in a random subset of the slots. 10 seeds (0-9); results are reported as mean and min-max.
- **Class-based ABC:** slots, sorted by walking distance, are split into three zones sized to the number of A, B and C SKUs (ABC by pick frequency from Module 1). A SKUs go to the nearest zone, then B, then C. Placement inside a zone is random (10 seeds). Spare slots stay empty at the far end. This is the most common practical policy.
- **Full velocity:** SKUs are sorted by pick frequency (ties by SKU code); the fastest SKU takes the nearest slot, the next fastest the next nearest, and so on. Deterministic.
- SKU ranking uses the order-line count from Module 1 (`pick_lines`); routing uses distinct (order, SKU) visits (517,254 visits from 527,725 lines).

## 7. Evaluation and KPIs (Modules 2 and 3)
- Every order of the last 12 months (19,773) is routed in every scenario; no sampling.
- **Avg. distance per order** = mean S-shape route length over all orders (m). **Total annual distance** = sum over all orders (km). For random and class-based scenarios both are means over the seeds.
- **Change vs. random baseline** = total distance of the scenario / mean total distance of the random seeds - 1.
- **Picks from nearest 20% of slots** = share of (order, SKU) visits served from the 800 slots closest to the depot by walking distance.
- **Estimated annual walking time** = total distance / 1.0 m/s. Walking only; picking, handling and congestion are excluded.
- `reports/kpi_summary.xlsx` holds the KPI table, all assumptions and the slot assignment of the 100 fastest SKUs. Its numbers are read from `data/processed/scenario_results.csv` and checked against it after saving.

## 8. Out-of-sample check (Module 4)
- **Purpose:** Modules 2-3 rank SKUs with the same period they are evaluated on. Module 4 measures how much of the saving remains when SKUs are ranked on past orders only, on two independent splits, and decomposes the gap into a new-SKU effect and a frequency-drift effect.

### 8.1 Splits
- **6+6 months** (within the Module 1 year): each invoice is assigned by its timestamp. Orders before 1 June 2011 are the *history* (8,067 orders, Dec 2010 - May 2011); orders on or after are the *future/evaluation* set (11,706 orders, Jun - Dec 2011). SKU universe = all 3,791 SKUs of the full year (Module 1's `sku_velocity.csv`), unchanged from the first version of this module. The two halves are unequal in order count and the evaluation half includes the autumn peak.
- **Year-over-year**: *history* = the raw sheet `Year 2009-2010`, cleaned with Module 1's rules and trimmed to invoices starting in [1 Dec 2009, 1 Dec 2010). *Future* = Module 1's `clean_lines.csv` (sheet `Year 2010-2011`), trimmed to invoices starting in [1 Dec 2010, 1 Dec 2011). Two full, non-overlapping 12-month windows. SKU universe = only SKUs picked in the future window (3,789 SKUs) - a warehouse does not reserve a slot for an item it has never stocked. If that count exceeded the default 4,000-slot layout (40 aisles), Module 4 would add aisles (`ceil(n_skus / (2 x 50))`) and log it; at 3,789 SKUs this was not needed, so both splits use the same 40-aisle layout.
- **Cross-sheet overlap:** the two source sheets share an exact 9-day window (1-9 Dec 2010) that is byte-for-byte identical in both (verified directly on the raw file). Before windowing, every history row is matched against the future rows on all business columns (Invoice, StockCode, Description, Quantity, InvoiceDate, Price, CustomerID, Country), matching the *k*-th occurrence of a repeated row to the *k*-th occurrence on the other side (so a value that legitimately repeats within one sheet is not mismatched); rows found in both sheets are dropped from history. This removed 21,932 rows (830 invoices) - all of them already outside the history window by date, so the explicit dedup did not change the final counts but is kept as a safeguard and is reported by the script.

### 8.2 Ranking, evaluation and visit scope
- **Out-of-sample:** pick frequency (order-line count per SKU, as in Module 1) and ABC classes (same 80% / 95% cut-offs, via Module 1's `abc_class`) are computed from the history orders only. SKUs without picks in the history get frequency 0 and class C, and (via `slot_full_velocity` / `slot_class_based`) are placed in the farthest slots. All SKUs of the split's universe still receive a slot.
- **In-sample reference:** the same computation on the future/evaluation orders themselves - not equal to Module 2's full-year results, since it ranks and evaluates on a different (or, for year-over-year, differently windowed) set of orders.
- **Evaluation:** the same layout, S-shape routing and 10 random seeds as Module 2, run on the future orders of each split. Reduction = 1 - distance of the scenario / mean distance of the random seeds evaluated on the same visits.
- **visit_scope = "seen_only_visits":** the same evaluation, restricted to visits of SKUs that had at least one pick in the history period, each against its own 10-seed random baseline computed on that restricted visit set. Orders made up entirely of unseen SKUs (298 of 11,706 for 6+6 months; 673 of 18,957 for year-over-year) have no visits left and drop out of this scope entirely - `n_orders_evaluated` in the output records this.
- **Decomposition:** `new_sku_effect_pp = reduction(seen_only_visits) - reduction(all_visits)`, in percentage points. It is the extra decline caused specifically by having to slot SKUs with no ranking history; the remaining gap between the seen-only reduction and the in-sample reduction is ordinary pick-frequency drift among already-known SKUs.
- **Caveats:** two splits, not a distribution of splits - treat the numbers as indicative, not a confidence interval. The seen-only comparison changes both the numerator (distance) and the order/visit set, so it isolates "what if there were no new SKUs", not a controlled experiment that holds everything else fixed.

## 9. Known limitations
- The layout is synthetic; absolute distances depend on its dimensions, relative differences between scenarios on the layout shape and order size.
- The main results (Modules 2-3) are in-sample, which favours the velocity-based scenarios; the out-of-sample check (section 8) shows a realistic saving is roughly half of the in-sample figures, and that most of the gap is attributable to SKUs with no pick history rather than to the ranking method itself.
- Zones for class-based slotting follow walking distance from the depot; aisle-based zoning and co-occurrence-based slotting were not tested.
- No batching, congestion, item size or rack capacity constraints, and no re-slotting cost.
