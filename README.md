# Warehouse Slotting & Picking Optimization

> Velocity-based slotting analysis on real order data to reduce picker travel distance, with a KPI framework to measure the improvement.

![Python](https://img.shields.io/badge/Python-3.10+-blue) ![pandas](https://img.shields.io/badge/pandas-data%20analysis-150458) ![Status](https://img.shields.io/badge/status-completed-brightgreen)

## Business Problem

In a warehouse with thousands of SKUs, most of a picker's time is spent **walking**, not picking. If fast-moving items are stored far from the dispatch area, every order costs extra travel time and labour.

**Question:** How much can picker travel distance be reduced by re-slotting SKUs based on how often they are picked?

## Key Results

Real orders (UCI Online Retail II, last 12 months: 19,773 orders, 3,791 SKUs) routed with the S-shape heuristic through a synthetic 40-aisle warehouse.

| Scenario | Avg. distance per order | Total annual distance | Change vs. random | Est. walking time¹ | Picks from nearest 20% of slots |
|---|---|---|---|---|---|
| Random (baseline)² | 933 m | 18,449 km | – | 5,125 h | 20.2% |
| Class-based ABC² | 707 m | 13,973 km | **-24.3%** | 3,882 h | 49.9% |
| Full velocity | 635 m | 12,547 km | **-32.0%** | 3,485 h | 65.3% |

¹ Total distance at an assumed walking speed of 1.0 m/s; walking only, no picking or handling time.
² Mean of 10 random seeds. Range across seeds: random 930-936 m per order, class-based 703-711 m.

- **Class-based ABC captures about three quarters of the full-velocity saving** with only three zones, which is why it is the more practical option in a real warehouse.
- Orders are large (mean 26 lines, median 15), so picks still spread over many aisles even after re-slotting; this limits the achievable saving.
- Slotting was ranked and evaluated on the same 12 months, so these figures are an upper bound for future orders (see the out-of-sample check below).

![Distance by scenario](reports/figures/03_distance_by_scenario.png)

![Pick density, random vs. full velocity](reports/figures/04_heatmap_random_vs_velocity.png)

Full tables: `reports/kpi_summary.xlsx` (KPI summary, assumptions, slot assignment of the 100 fastest SKUs).

### Out-of-sample check (Module 4)

The table above ranks SKUs with the same orders it evaluates. Module 4 checks how much of the saving survives when SKUs are ranked on *earlier* orders only, on two independent splits (same layout and S-shape routing in both):

- **6+6 months** (within the last 12 months): rank on Dec 2010 - May 2011 (8,067 orders), evaluate on Jun - Dec 2011 (11,706 orders). Unequal halves; the evaluation half includes the autumn peak.
- **Year-over-year**: rank on the whole prior year, 1 Dec 2009 - 30 Nov 2010 (19,743 orders), evaluate on the whole following year, 1 Dec 2010 - 30 Nov 2011 (18,957 orders) - two equal, non-overlapping 12-month windows. Only SKUs actually picked in the evaluated year are slotted (3,789 SKUs, still under the 4,000-slot layout). The two source sheets share an exact 9-day overlap at their boundary (21,932 duplicate rows / 830 invoices), which is detected and removed before the split.

| Scenario | Split | In-sample reduction | Out-of-sample reduction |
|---|---|---|---|
| Class-based ABC | 6+6 months | -26.9% | **-13.3%** |
| Class-based ABC | Year-over-year | -24.4% | **-12.6%** |
| Full velocity | 6+6 months | -35.0% | **-18.3%** |
| Full velocity | Year-over-year | -32.0% | **-18.6%** |

The two splits - built from non-overlapping years of data - agree closely (class-based -13.3% vs. -12.6%; full velocity -18.3% vs. -18.6%). **We treat the year-over-year split as the primary out-of-sample estimate**, because it compares two equal 12-month windows (no seasonal imbalance between the ranking and evaluation periods) and matches how a warehouse would actually re-slot - once a year, on the prior year's data. The 6+6 split is a robustness check and reaches the same conclusion.

**Where does the rest of the in-sample saving go?** Each out-of-sample scenario was evaluated a second time, excluding visits to SKUs never picked in the ranking period ("seen-only visits"), against its own random baseline computed the same way:

| Scenario | Split | Reduction, all visits | Reduction, seen-only visits | New-SKU effect |
|---|---|---|---|---|
| Class-based ABC | 6+6 months | -13.3% | -23.8% | 10.5 pp |
| Class-based ABC | Year-over-year | -12.6% | -23.8% | 11.3 pp |
| Full velocity | 6+6 months | -18.3% | -31.4% | 13.1 pp |
| Full velocity | Year-over-year | -18.6% | -31.9% | 13.3 pp |

- SKUs with no pick history in the ranking period (514 of 3,791 SKUs / 16.2% of visits, 6+6; 651 of 3,789 SKUs / 18.7% of visits, year-over-year) are ranked last by construction. Excluding their visits recovers most of the in-sample saving: seen-only reductions (23.8-31.9%) sit close to the in-sample figures (24.4-35.0%).
- So **10-13 percentage points of the out-of-sample gap is specifically the cost of slotting items with no pick history**; the remaining 1-4 points is ordinary pick-frequency drift among SKUs that were already known.
- Practically, this argues for a new-SKU policy (provisional slotting from a category/supplier proxy, then a fast re-check after the first weeks of sales) rather than against velocity-based slotting itself.

![In-sample vs. out-of-sample](reports/figures/05_in_vs_out_of_sample.png)

### Order profile (Module 1)

- 33.6% of SKUs generate 80% of pick lines. 28.7% of SKUs land in a different ABC class by revenue than by pick frequency.
- 7.6% of orders have a single line; the median order has 15 lines.

![Lines per order](reports/figures/01_lines_per_order.png)

![Pick frequency Pareto](reports/figures/02_pick_pareto.png)

## Approach

| # | Module | Status |
|---|---|---|
| 1 | **Order profile & velocity ABC:** lines per order, pick-frequency Pareto, ABC by pick frequency vs. ABC by revenue | ✅ Done |
| 2 | **Slotting & routing:** synthetic warehouse layout, random / class-based ABC / full-velocity slotting, S-shape picking route distance | ✅ Done |
| 3 | **KPI framework:** before/after comparison of travel distance and related warehouse KPIs in an Excel workbook | ✅ Done |
| 4 | **Out-of-sample check:** two independent history/future splits, plus a new-SKU vs. frequency-drift decomposition | ✅ Done |

### Why pick frequency instead of revenue?
A high-revenue SKU is not necessarily a frequently picked SKU. Slotting decisions should be driven by **how many times** an item is picked, because each pick line means one trip to the location.

## Project Structure

```
warehouse-slotting-optimization/
├── data/                 # Data source & download instructions (raw and processed files not committed)
├── src/                  # Analysis scripts, run in order
├── tests/                # Unit tests for the routing distance and slotting policies
├── reports/              # kpi_summary.xlsx and figures/
├── docs/                 # Methodology notes
└── requirements.txt
```

## How to Run

```bash
pip install -r requirements.txt
# Download the dataset (see data/README.md), then run the scripts in order:
python src/01_order_profile_abc.py     # order profile, ABC classes, cleaned lines
python src/02_slotting_routing.py      # layout, 3 slotting scenarios, S-shape distances (~10 s)
python src/03_kpi_summary.py           # reports/kpi_summary.xlsx
python src/04_out_of_sample_check.py   # in-sample vs. out-of-sample savings (~10 s)

# Tests (the S-shape distance is checked against hand-calculated routes)
python -m pytest tests/                # or: python tests/test_routing.py
```

## Assumptions & Limitations

- Order data is **real** (UCI Online Retail II); the warehouse layout is **synthetic**, since the source retailer's layout is not public. Results depend on the layout parameters (constants at the top of `src/02_slotting_routing.py`).
- Layout: 40 aisles x 2 racks x 50 slots (4,000 slots for 3,791 SKUs), 50 m aisles, 3 m aisle pitch, depot at the left end of the front cross aisle. Cross-aisle width and lateral movement inside an aisle are ignored.
- Picking routes use the S-shape heuristic, a common industry baseline rather than an optimal route. One picker per order; no batching, congestion or zone picking.
- A pick is one distinct (order, SKU) visit. Random and class-based results are averages over 10 seeds; all 19,773 orders are evaluated (no sampling).
- Walking time assumes 1.0 m/s and covers walking only; it is an indicator, not a labour-hours forecast.
- **In-sample main results:** the Key Results rank SKUs with the same period they are evaluated on. Module 4 checks two independent splits (within-year and year-over-year) and both agree: a realistic saving is roughly -13% (class-based) and -18% (full velocity), about half the in-sample figures above. 10-13 percentage points of that gap is specifically the cost of slotting SKUs with no pick history (see the out-of-sample check below), not a flaw in velocity-based slotting itself.
- Item dimensions, weight, rack capacity, replenishment and the cost of re-slotting are not modelled.

## Author

**Oğuzhan Ketenci**, Industrial Engineering, Yıldız Technical University
[LinkedIn](https://linkedin.com/in/oguzhanketenci)
