"""
Collect the real numbers the LinkedIn video shows (read-only: imports the analysis modules and
reads their outputs; changes nothing). Writes reports/video/video_data.json.

Run: python reports/video/make_video_data.py   (after Modules 1-11b)
"""
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
PROC = ROOT / "data" / "processed"
OUT = Path(__file__).resolve().parent / "video_data.json"


def load(filename, name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "src" / filename)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


m11b = load("11b_new_product_rule.py", "new_product_rule")
sr, m5, m7 = m11b.sr, m11b.m5, m11b.m7
m4 = load("04_out_of_sample_check.py", "out_of_sample_check")
m1 = load("01_order_profile_abc.py", "order_profile_abc")
m8b = load("08b_congestion_refined.py", "congestion_refined")
REC = "Aisle-based velocity + new-product rule"
data = {}

# 1. Next-year walking per layout (year-over-year, S-shape) and per route on the recommended layout
npr = pd.read_csv(PROC / "new_product_rule_routing.csv")
y = npr[npr["split"] == "year-over-year"]
s = y[y["route"] == "S-shape"].set_index("scenario")
data["ladder"] = [{"scenario": k, "m": float(s.loc[k, "avg_distance_per_order_m"]),
                   "saving_pct": float(-100 * s.loc[k, "change_vs_random"])}
                  for k in ("Random (baseline)", "Class-based ABC", "Full velocity", "Aisle-based velocity", REC)]
r = y[y["scenario"] == REC].set_index("route")["avg_distance_per_order_m"]
data["routes_recommended_m"] = {k: float(v) for k, v in r.items()}
data["optimal_vs_sshape_pct"] = float(100 * (1 - r["Optimal"] / r["S-shape"]))
data["largest_gap_vs_optimal_pct"] = float(100 * (r["Largest gap"] / r["Optimal"] - 1))
rs = pd.read_csv(PROC / "routing_sensitivity.csv")
data["aisle_same_year_pct"] = float(-100 * rs[(rs["routing"] == "S-shape") & (rs["scenario"] == "Aisle-based velocity")]["change_vs_random"].item())

# 2. Year-over-year history: ABC class shares, and the evaluation-year order count
hy, fy, _ = m4.year_over_year_periods(m1)
codes = sorted(fy["StockCode"].unique())
layout, _ = m4.size_layout(sr, len(codes))
idx = pd.Series(np.arange(len(codes)), index=codes)
vo, vs = m4.order_visits(fy, idx)
pick, cls = m4.sku_stats(hy, codes, m1.abc_class)
n_a = int((cls == "A").sum())
data["abc_shares"] = {c: float((cls == c).mean()) for c in "ABC"}
data["next_year_orders"] = int(vo.max()) + 1

# 3. Four real orders on the recommended layout where each simple rule is the best (WMS choice)
ranks = m11b.aisle_based(pick, n_a, True, layout)
a, yy = m7.pick_coordinates(ranks, layout, vs)
d = {"s": sr.s_shape_distances(vo, a, yy), "r": sr.return_distances(vo, a, yy),
     "g": sr.largest_gap_distances(vo, a, yy), "c": m7.combined_distances(vo, a, yy)}
dist = pd.DataFrame(d)
side = layout["side"].to_numpy()[ranks[vs]]
visits = pd.DataFrame({"o": vo, "aisle": a, "y": yy, "side": side})
lines = visits.groupby("o").size()
n_aisles = visits.groupby("o")["aisle"].nunique()
dmin = dist.min(axis=1).to_numpy()
orders = []
# Combined is never longer than S-shape or Return (Module 7), so for those two the rule must tie for best
# (Combined then takes the same route); the margin is measured against the other rules.
for w, want in (("r", 3), ("s", 4), ("g", 7), ("c", 6)):                  # storyboard: winner, line count
    others = [k for k in "srgc" if k != w and not (w in "rs" and k == "c")]
    margin = dist[others].min(axis=1).to_numpy() / dist[w].to_numpy() - 1
    ok = (dist[w].to_numpy() <= dmin + 1e-6) & (margin >= 0.03) & (n_aisles.to_numpy() <= 5) & (n_aisles.to_numpy() >= 2)
    gap = np.where(ok, np.abs(lines.to_numpy() - want), 10**9)        # nearest line count, then lowest index
    o = int(np.argmin(gap))
    assert ok[o], f"no order where {w} is best"
    p = visits[visits["o"] == o]
    orders.append({"winner": w, "order_index": o, "lines": int(lines[o]), "aisles": int(n_aisles[o]),
                   "m": {k: float(dist.loc[o, k]) for k in "srgc"}, "margin_pct": float(100 * margin[o]),
                   "picks": [{"aisle": int(q.aisle), "y_m": float(q.y), "side": int(q.side)} for q in p.itertuples()]})
data["example_orders"] = orders
data["example_orders_rule"] = ("first order (by index) on the recommended layout, year-over-year evaluation year, "
                               "with the storyboard line count (else the nearest), the given rule best (Return and S-shape tie with Combined) and >= 3% shorter than the other rules, 2-5 aisles")

# 4. Congestion (Modules 8a and 8b, x10, 23 pickers, Aisle-based velocity, S-shape)
c8a = pd.read_csv(PROC / "congestion_simulation.csv")
c8a = c8a[(c8a["case"] == "base") & (c8a["volume"] == 10) & (c8a["pickers"] == 23) & (c8a["route"] == "S-shape")
          & (c8a["scenario"] == "Aisle-based velocity")].iloc[0]
data["jam_wait_min"] = float(c8a["aisle_wait_per_order_s"] / 60)
data["jam_front5_pct"] = float(c8a["front_aisles_wait_share_pct"])
c8b = pd.read_csv(PROC / "congestion_refined.csv")
c8b = c8b[(c8b["volume"] == 10) & (c8b["pickers"] == 23) & (c8b["aisle_model"] == "narrow")
          & (c8b["scenario"] == "Aisle-based velocity")].drop_duplicates(["policy"]).set_index("policy")
data["skip_wait_reduction_pct"] = float(100 * (1 - c8b.loc["skip", "aisle_wait_per_order_s"] / c8b.loc["wait", "aisle_wait_per_order_s"]))
data["segment_wait_drop_x"] = "8-9x (README Module 8b, reference point)"

# 5. Staffing (Module 11, x1, Aisle-based velocity)
st = pd.read_csv(PROC / "staffing_by_layout_x1.csv")
st = st[st["scenario"] == "Aisle-based velocity"]
a6 = st[(st["pickers"] == 6) & (st["cutoff_h"] == 24)].iloc[0]
a4 = st[(st["pickers"] == 4) & (st["cutoff_h"] == 18)].iloc[0]
data["staffing"] = {"pickers_before": 6, "pickers_after": 4, "cost_before": float(a6["cost_units_per_order"]),
                    "cost_after": float(a4["cost_units_per_order"]),
                    "cost_cut_pct": float(100 * (1 - a4["cost_units_per_order"] / a6["cost_units_per_order"]))}

# 6. Real orders by hour (x1 arrivals used by Modules 8a-8c and 11), average per working day
arr = m8b.arrivals_for(1)
hour = (arr["t"].to_numpy() // 3600).astype(int)
days = arr["date"].nunique()
h = pd.Series(hour).value_counts().sort_index()
data["orders_by_hour"] = {"days": int(days), "per_day": {int(k): float(v / days) for k, v in h.items()},
                          "after_18_share_pct": float(100 * (hour >= 18).mean()), "orders": int(len(arr))}

OUT.write_text(json.dumps(data, indent=1), encoding="utf-8")
print(json.dumps({k: v for k, v in data.items() if k != "example_orders"}, indent=1))
for o in orders:
    print(o["winner"], o["lines"], o["aisles"], {k: round(v) for k, v in o["m"].items()}, round(o["margin_pct"], 1))
