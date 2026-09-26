# Warehouse Slotting & Picking Optimization

> Velocity-based slotting analysis on real order data to reduce picker travel distance, with a KPI framework to measure the improvement.

![Python](https://img.shields.io/badge/Python-3.10+-blue) ![pandas](https://img.shields.io/badge/pandas-data%20analysis-150458) ![Status](https://img.shields.io/badge/status-in%20progress-orange)

## Business Problem

In a warehouse with thousands of SKUs, most of a picker's time is spent **walking**, not picking. If fast-moving items are stored far from the dispatch area, every order costs extra travel time and labour.

**Question:** How much can picker travel distance be reduced by re-slotting SKUs based on how often they are picked?

## Key Results

| Metric | Baseline | Optimized | Change |
|---|---|---|---|
| Avg. travel distance per order | TBD | TBD | TBD |
| Total annual travel distance | TBD | TBD | TBD |

*To be updated as each module is completed.*

## Approach

| # | Module | Status |
|---|---|---|
| 1 | **Order profile & velocity ABC:** lines per order, pick-frequency Pareto, ABC by pick frequency vs. ABC by revenue | ✅ Done |
| 2 | **Slotting & routing:** synthetic warehouse layout, random (baseline) vs. velocity-based slotting, S-shape picking route distance | ⏳ In progress |
| 3 | **KPI framework:** before/after comparison of travel distance and related warehouse KPIs | ⏳ Planned |

### Why pick frequency instead of revenue?
A high-revenue SKU is not necessarily a frequently picked SKU. Slotting decisions should be driven by **how many times** an item is picked, because each pick line means one trip to the location.

## Project Structure

```
warehouse-slotting-optimization/
├── data/                 # Data source & download instructions (raw files not committed)
├── src/                  # Analysis scripts, run in order
├── reports/figures/      # Generated charts
├── docs/                 # Methodology notes
└── requirements.txt
```

## How to Run

```bash
pip install -r requirements.txt
# Download the dataset (see data/README.md), then:
python src/01_order_profile_abc.py
```

## Assumptions & Limitations

- Order data is **real** (UCI Online Retail II); the warehouse layout is **synthetic**, since the source retailer's layout is not public.
- Picking routes use the S-shape heuristic, a common industry baseline rather than an optimal route.
- Item dimensions and storage constraints are not modelled in the first version.

## Author

**Oğuzhan Ketenci**, Industrial Engineering, Yıldız Technical University
[LinkedIn](https://linkedin.com/in/oguzhanketenci)
