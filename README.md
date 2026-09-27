# Warehouse Slotting & Picking Optimization

> Velocity-based slotting analysis on real order data to reduce picker travel distance, with a KPI framework to measure the improvement.

![Python](https://img.shields.io/badge/Python-3.10+-blue) ![pandas](https://img.shields.io/badge/pandas-data%20analysis-150458) ![Status](https://img.shields.io/badge/status-completed-brightgreen)

## TL;DR

- **Full-year, in-sample** (Modules 2-3): re-slotting cuts average picker travel distance by **-24.3%** (class-based ABC) to **-32.0%** (full velocity) vs. a random layout.
- **Year-over-year, out-of-sample** (Module 4 - rank on the prior year, evaluate on the next): the realistic saving is roughly **-13%** (class-based) to **-19%** (full velocity); most of the gap versus the in-sample figures is the cost of slotting brand-new SKUs, not a weaker ranking.
- **Recommendation (updated by Module 6's move-cost analysis):** below roughly 4 minutes of labour per SKU moved, a threshold-based hybrid re-slotting policy - the nearest ~500 SKUs individually placed by velocity, the rest zoned A/B/C, only SKUs that cross a class or top-500 boundary get physically moved - gives the lowest total cost (walking + move labour). Above ~4 minutes, a one-time full-velocity ranking that is *never* updated again is cheaper, because it already beats the previously recommended static class-based ABC on distance at zero ongoing moves. Either way, pair it with a new-SKU rule (place unseen SKUs at the front of the B zone, not the back).

## Business Problem

In a warehouse with thousands of SKUs, most of a picker's time is spent **walking**, not picking. If fast-moving items are stored far from the dispatch area, every order costs extra travel time and labour.

**Question:** How much can picker travel distance be reduced by re-slotting SKUs based on how often they are picked?

## Key Results (Modules 2 & 3)

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

Module 3 – Full tables: `reports/kpi_summary.xlsx` (KPI summary, assumptions, slot assignment of the 100 fastest SKUs).

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

### Routing sensitivity (Module 5)

Everything above assumes **S-shape** routing (every aisle with a pick is walked end to end). Module 5 asks: **does the best slotting method change with the routing policy?** It adds two more routing heuristics - **Return** (every aisle is entered from the front and left the way it came, never traversed end to end) and **Largest gap** (the first and last aisle are traversed end to end; every aisle between them skips its single largest unpicked gap, entered from both ends) - and a 4th, simpler slotting rule, **Aisle-based velocity** (the fastest SKUs fill the nearest aisle completely before moving to the next, rather than being ranked by exact walking distance). All 19,773 orders of the last 12 months, same layout as above.

| Routing policy | Random | Class-based ABC | Full velocity | Aisle-based velocity |
|---|---|---|---|---|
| S-shape | 933 m | 707 m (-24.3%) | 635 m (-32.0%) | **587 m (-37.1%)** |
| Return | 1,051 m | 714 m (-32.0%) | **593 m (-43.6%)** | 695 m (-33.9%) |
| Largest gap | 724 m | 579 m (-20.0%) | 510 m (-29.6%) | **493 m (-31.9%)** |

*(% = reduction vs. the random baseline of the same routing policy - absolute distances are not comparable across policies. Bold = shortest non-random scenario per row.)*

![Routing sensitivity](reports/figures/06_routing_sensitivity.png)

- **Yes, the best-performing scenario changes with the routing policy.** Aisle-based velocity is shortest under S-shape and Largest gap; Full velocity is shortest under Return (where Aisle-based velocity is worse than Full velocity, but still beats Class-based ABC).
- **Why:** S-shape and Largest gap charge a cost per aisle visited that is close to fixed (a full traversal, or up to the largest gap) almost regardless of pick depth, so minimising the *number of aisles touched* - what Aisle-based velocity does by packing fast SKUs into complete aisles - matters more than minimising raw walking distance. Return's cost is a direct there-and-back distance to each pick, exactly what Full velocity's ranking (straight-line distance from the depot) is built to minimise.
- **Class-based ABC stays a robust middle choice under all three policies** (-20.0% to -32.0% vs. random) - this is why the recommendation above does not depend on knowing which routing heuristic a real warehouse uses. The choice between the two more precise methods (Full velocity vs. Aisle-based velocity) does depend on it: prefer Full velocity if pickers tend to backtrack (Return-like), Aisle-based velocity - a simpler rule than a full distance ranking - if they loop through aisles (S-shape/Largest-gap-like).

### Re-slotting policies (Module 6)

Every result above compares one-time layouts. Real warehouses keep operating while pick frequencies drift, so re-slotting has a recurring cost: every SKU that changes slot has to be physically moved. Module 6 simulates a full evaluation year (Dec 2010 - Nov 2011, Module 4's year-over-year setup) month by month, updating each policy's layout using only a rolling 12-month window of data available *as of* that month (no look-ahead), and counts how many SKUs move at each update.

| Policy | Params | Avg. distance/order | Annual moves | Total h/year @ 2 min/move | @ 5 min/move | @ 10 min/move |
|---|---|---|---|---|---|---|
| Random (baseline) | – | 933 m | 0 | 4,913 | 4,913 | 4,913 |
| Oracle (look-ahead)¹ | – | 634 m | 0 | 3,340 | 3,340 | 3,340 |
| P1 Static ABC | – | 816 m | 0 | 4,296 | 4,296 | 4,296 |
| P2 Static full velocity | – | 760 m | 0 | 4,001 | **4,001** | **4,001** |
| P3 Hybrid static | N=500 (best of 50-500) | 783 m | 0 | 4,124 | 4,124 | 4,124 |
| P4 Periodic full velocity | monthly | 691 m | 40,161 | 4,975 | 6,983 | 10,330 |
| P4 Periodic full velocity | quarterly | 722 m | 11,232 | 4,176 | 4,738 | 5,674 |
| P5 Periodic hybrid | N=500, monthly | 728 m | 2,300 | 3,911 | 4,026 | 4,217 |
| P5 Periodic hybrid | N=500, quarterly | 747 m | 1,419 | 3,979 | 4,050 | 4,168 |
| P6 Threshold-based | N=500, T=25 | 725 m | 4,131 | 3,955 | 4,161 | 4,506 |
| P6 Threshold-based | N=500, T=50 | 724 m | 3,274 | 3,923 | 4,087 | 4,360 |
| P6 Threshold-based | N=500, T=100 | 725 m | 2,706 | **3,910** | 4,046 | 4,271 |
| P7 P1 + new-SKU-to-B | – | 764 m | 0 | 4,025 | 4,025 | 4,025 |

¹ Full velocity ranked on the evaluation year's own frequency - not achievable without foresight, an upper bound only, not a candidate policy. P7's own totals (0 moves, so constant across columns) are shown for the new-SKU comparison; **P2 (4,001 h, also constant) is the actual cheapest policy at 5 and 10 min/move**, ahead of every throttled hybrid - see the sensitivity discussion below. Bold = lowest total cost per column among all policies (not shown again for P2's repeated 4,001 at 5/10 min to avoid double-bolding).

![Re-slotting trade-off](reports/figures/07_reslotting_tradeoff.png)

**Does the best policy change with the assumed move cost, and how sensitive is the answer?** Yes, and the crossover is precise: **P6 (threshold-based, N=500, T=100)** and **P5 (periodic hybrid, N=500, monthly)** are in a practical tie for cheapest at 2 minutes/move (3,910 h vs. 3,911 h/year); **P2 (static full velocity, never updated)** overtakes both once a move costs more than about **4.0-4.4 minutes** of labour - below that, a small number of well-targeted moves (2,300-2,706/year - 93-94% fewer than P4's 40,161 full monthly re-rank) pays for itself; above it, doing nothing after the initial ranking wins.

- **Plain class-based ABC (P1) is dominated at every move-time tested** - both P2 and every throttled hybrid (P5/P6) beat it on total cost, since P1's only advantage was assumed operational simplicity, and a one-time full-velocity ranking is no harder to set up once. This is why the TL;DR recommendation was updated (see above).
- **Full monthly re-ranking (P4) is never worth it in this cost range**: even at 2 minutes/move it costs 4,975 h/year (worse than every throttled or static alternative), because strict individual velocity ranking reshuffles most of the catalogue's long tail every month (40,161 moves/year, ~96% of SKUs on average) from small frequency ties shifting - a known weakness of ranking every SKU individually rather than in zones.
- **The new-SKU rule (P7) meaningfully helps a static ABC layout** (764 m vs. 816 m for plain P1) but still doesn't close the gap to full velocity (760 m) or the throttled hybrids (~725-728 m); applying the same rule to P4 (monthly) makes no difference, because pure full-velocity ranking has no B zone for a new SKU to be inserted into.
- N=500 was the best of the tested grid {50, 100, 200, 300, 500} for both the static hybrid (P3) and the N reused by P5/P6 - the improvement was still increasing at N=500, so a larger N might do even better; this was not tested (see methodology).

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
| 5 | **Routing sensitivity:** Return and largest-gap routing policies, a 4th aisle-based-velocity scenario, 4x3 evaluation grid | ✅ Done |
| 6 | **Re-slotting policies:** 7 update policies simulated month by month with a move-cost/distance trade-off | ✅ Done |

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
python src/05_routing_sensitivity.py   # 4 scenarios x 3 routing policies (~20 s)
python src/06_reslotting_policies.py   # 7 re-slotting policies, simulated month by month (~4 min)

# Tests (the S-shape distance is checked against hand-calculated routes)
python -m pytest tests/                # or: python tests/test_routing.py
```

## Assumptions & Limitations

- Order data is **real** (UCI Online Retail II); the warehouse layout is **synthetic**, since the source retailer's layout is not public. Results depend on the layout parameters (constants at the top of `src/02_slotting_routing.py`).
- Layout: 40 aisles x 2 racks x 50 slots (4,000 slots for 3,791 SKUs), 50 m aisles, 3 m aisle pitch, depot at the left end of the front cross aisle. Cross-aisle width and lateral movement inside an aisle are ignored.
- Picking routes use the S-shape heuristic by default, a common industry baseline rather than an optimal route; Module 5 cross-checks the main scenarios under two more heuristics (Return, Largest gap) and finds the best *precise* method depends on which one a warehouse actually uses (see the routing sensitivity check above), while class-based ABC stays robust across all three. One picker per order; no batching, congestion or zone picking.
- A pick is one distinct (order, SKU) visit. Random and class-based results are averages over 10 seeds; all 19,773 orders are evaluated (no sampling).
- Walking time assumes 1.0 m/s and covers walking only; it is an indicator, not a labour-hours forecast.
- **In-sample main results:** the Key Results rank SKUs with the same period they are evaluated on. Module 4 checks two independent splits (within-year and year-over-year) and both agree: a realistic saving is roughly -13% (class-based) and -18% (full velocity), about half the in-sample figures above. 10-13 percentage points of that gap is specifically the cost of slotting SKUs with no pick history (see the out-of-sample check above), not a flaw in velocity-based slotting itself.
- Item dimensions, weight, rack capacity and replenishment are not modelled. The labour cost of re-slotting itself *is* modelled in Module 6 (move counts x an assumed minutes-per-move), but only as a simple linear cost - no truck/labour scheduling, batching of moves, or SKU handling difficulty.
- Module 6's rolling-window monthly updates assume a warehouse can recompute pick frequency and physically execute moves essentially overnight; it does not model the lag between deciding to re-slot and completing the move.

## Author

**Oğuzhan Ketenci**, Industrial Engineering, Yıldız Technical University
[LinkedIn](https://linkedin.com/in/oguzhanketenci)
