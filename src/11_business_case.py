"""
Module 11 - Business Case: staffing per layout at today's volume, summary figures

1. Module 8c's cut-off simulation (narrow aisles, S-shape, wait, carry-over, same service level)
   at x1 volume for all 4 layouts, with an 18:00 cut-off and without one (24:00 = Module 8b's
   cost-based service level), teams 3, 4, 5, 6, 8. Without a cut-off, Class-based ABC and Full
   velocity must reproduce Module 8b's stored rows exactly.
2. Out-of-sample check for Aisle-based velocity on Module 4's two splits (Module 4's code re-run;
   its stored rows must be reproduced exactly).
3. Summary figures for the README (no new results; all numbers read from CSVs; panel 1's
   next-year bars come from src/11b_new_product_rule.py, which must be run first).

Input : as Modules 4 and 8a-8c; data/processed/{routing_sensitivity,new_product_rule_routing,
        congestion_simulation,congestion_refined}.csv
Run   : python src/11_business_case.py --estimate
        python src/11_business_case.py
        python src/11_business_case.py --out-of-sample
        python src/11_business_case.py --plot-only
Output:
  data/processed/staffing_by_layout_x1.csv
  data/processed/aisle_out_of_sample.csv
  reports/figures/00_summary.png
  reports/figures/linkedin_summary.png
"""
import argparse
import importlib.util
import os
import time
from multiprocessing import Pool
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"
FIG = ROOT / "reports" / "figures"


def load_module(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "src" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m8c = load_module("08c_cutoff_service.py", "cutoff_service")
m8b, sr, m5 = m8c.m8b, m8c.sr, m8c.m5

LAYOUTS = (sr.SCENARIO_RANDOM, sr.SCENARIO_CLASS, sr.SCENARIO_VELOCITY, m5.SCENARIO_AISLE)
CUTOFFS = (18, m8c.NO_CUTOFF_H)
TEAMS = (3, 4, 5, 6, 8)
EST_S = 35                                               # single-process seconds per x1 configuration


# ----------------------------------------------------------------------------
# Staffing per layout (x1)
# ----------------------------------------------------------------------------
def configs():
    return [{"volume": 1, "pickers": n, "scenario": s, "policy": "wait", "cutoff_h": c}
            for s in LAYOUTS for c in CUTOFFS for n in TEAMS]


def _run(cfg):
    return m8c.run_config(cfg)                           # module-level wrapper so Pool can pickle it


def check_8b(res: pd.DataFrame) -> None:
    """No cut-off must reproduce Module 8b's stored x1 team-size rows (ABC, Full velocity)."""
    ref = pd.read_csv(PROC / "congestion_refined.csv")
    ref = ref[(ref["volume"] == 1) & (ref["aisle_model"] == "narrow") & (ref["policy"] == "wait")
              & ref["set"].str.contains("team_size")]
    mine = res[res["cutoff_h"] == m8c.NO_CUTOFF_H]
    m = mine.merge(ref, on=["pickers", "scenario"], suffixes=("", "_8b"))
    assert len(m) == 2 * len(TEAMS), f"expected {2 * len(TEAMS)} matching 8b rows, got {len(m)}"
    pairs = [("aisle_wait_per_order_s", "aisle_wait_per_order_s_8b"), ("cycle_time_min", "cycle_time_min_8b"),
             ("overtime_work_picker_h_per_day", "overtime_work_picker_h_per_day_8b"),
             ("cost_units_per_order", "cost_units_per_order_8b"), ("days_overtime_ok_pct", "days_overtime_ok_pct_8b")]
    diff = max(float((m[a] - m[b]).abs().max()) for a, b in pairs)
    same_sla = bool((m["meets_sla"] == m["meets_sla_8b"].astype(bool)).all())
    if diff > 1e-9 or not same_sla:
        raise AssertionError(f"no cut-off does not reproduce Module 8b (max |diff| {diff}, same SLA {same_sla})")
    print(f"Check OK: no cut-off reproduces Module 8b's {len(m)} x1 rows (ABC, Full velocity) on "
          f"{len(pairs)} metrics + service level (max |diff| {diff:.1e}).")


def smallest_teams(res: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (s, c), g in res.groupby(["scenario", "cutoff_h"], sort=False):
        g = g.sort_values("pickers").reset_index(drop=True)
        ok = np.flatnonzero(g["meets_sla"].to_numpy())
        k = int(ok[0]) if len(ok) else None
        rows.append({"scenario": s, "cutoff": "none" if c == m8c.NO_CUTOFF_H else f"{c}:00",
                     "smallest_team": int(g.loc[k, "pickers"]) if k is not None else np.nan,
                     "fails_at": int(g.loc[k - 1, "pickers"]) if k else np.nan,
                     "cost_units_per_order": g.loc[k, "cost_units_per_order"] if k is not None else np.nan,
                     "cycle_time_min": g.loc[k, "cycle_time_min"] if k is not None else np.nan,
                     "carried_over_share_pct": g.loc[k, "carried_over_share_pct"] if k is not None else np.nan})
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# Out-of-sample check for Aisle-based velocity (Module 4's splits and code)
# ----------------------------------------------------------------------------
def aisle_out_of_sample() -> pd.DataFrame:
    """Module 4's two splits with Aisle-based velocity added (in-sample and out-of-sample, both
    visit scopes). Module 4's own scenarios are re-run with its code and must reproduce
    out_of_sample_results.csv exactly; only the Aisle-based rows are new."""
    m4 = load_module("04_out_of_sample_check.py", "out_of_sample_check")
    m1 = load_module("01_order_profile_abc.py", "order_profile_abc")
    h6, f6, _ = m4.within_year_periods()
    codes6 = sorted(pd.read_csv(PROC / "sku_velocity.csv", dtype={"StockCode": str})["StockCode"])
    hy, fy, _ = m4.year_over_year_periods(m1)
    codesy = sorted(fy["StockCode"].unique())
    runs = []
    for split, hist, fut, codes in ((m4.SPLIT_6_6, h6, f6, codes6), (m4.SPLIT_YOY, hy, fy, codesy)):
        base, _ = m4.run_split(sr, m1.abc_class, split, hist, fut, codes)
        layout, _ = m4.size_layout(sr, len(codes))
        near_cut = int(round(sr.NEAR_SLOT_SHARE * len(layout)))
        idx = pd.Series(np.arange(len(codes)), index=codes)
        vo, vs = m4.order_visits(fut, idx)
        hist_pick, _ = m4.sku_stats(hist, codes, m1.abc_class)
        eval_pick, _ = m4.sku_stats(fut, codes, m1.abc_class)
        seen = hist_pick[vs] > 0
        rows = []
        for basis, pick in ((m4.BASIS_IN, eval_pick), (m4.BASIS_OUT, hist_pick)):
            ranks = m5.slot_aisle_based_velocity(pick, layout)
            scopes = ((m4.SCOPE_ALL, vo, vs),) + (((m4.SCOPE_SEEN, vo[seen], vs[seen]),) if basis == m4.BASIS_OUT else ())
            for scope, o, s in scopes:
                avg, km, near = sr.evaluate(ranks, layout, o, s, near_cut)
                rows.append({"split": split, "scenario": m5.SCENARIO_AISLE, "ranking_basis": basis, "visit_scope": scope,
                             "seed": None, "avg_distance_per_order_m": avg, "total_distance_km": km,
                             "share_picks_nearest_20pct_slots": near, "n_orders_evaluated": len(np.unique(o))})
        runs += [base, pd.DataFrame(rows)]
    runs = pd.concat(runs, ignore_index=True)
    order = []
    for split in (m4.SPLIT_6_6, m4.SPLIT_YOY):
        order += [(split, sr.SCENARIO_RANDOM, m4.BASIS_NONE, m4.SCOPE_ALL), (split, sr.SCENARIO_RANDOM, m4.BASIS_NONE, m4.SCOPE_SEEN)]
        for s in (sr.SCENARIO_CLASS, sr.SCENARIO_VELOCITY, m5.SCENARIO_AISLE):
            order += [(split, s, m4.BASIS_IN, m4.SCOPE_ALL), (split, s, m4.BASIS_OUT, m4.SCOPE_ALL),
                      (split, s, m4.BASIS_OUT, m4.SCOPE_SEEN)]
    res = m4.summarize(runs, sr.SCENARIO_RANDOM, order)
    ref = pd.read_csv(PROC / "out_of_sample_results.csv")
    keys = ["split", "visit_scope", "scenario", "ranking_basis"]
    m = res.merge(ref, on=keys, suffixes=("", "_m4"))
    assert len(m) == len(ref), f"expected {len(ref)} Module 4 rows, matched {len(m)}"
    diff = max(float((m[c] - m[f"{c}_m4"]).abs().max()) for c in ("avg_distance_per_order_m", "change_vs_random"))
    if diff > 1e-9:
        raise AssertionError(f"re-run does not reproduce Module 4 (max |diff| {diff})")
    print(f"Check OK: re-run reproduces all {len(ref)} Module 4 rows (max |diff| {diff:.1e}).")
    return res[res["scenario"] == m5.SCENARIO_AISLE].reset_index(drop=True)


# ----------------------------------------------------------------------------
# Summary figure (numbers read from earlier modules' CSVs, nothing recomputed)
# ----------------------------------------------------------------------------
INK, INK_MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"
BEFORE, AFTER = "#c3c2b7", "#1baf7a"
SHORT = {sr.SCENARIO_RANDOM: "Random", sr.SCENARIO_CLASS: "Class-based\nABC", sr.SCENARIO_VELOCITY: "Full\nvelocity",
         m5.SCENARIO_AISLE: "Aisle-based\nvelocity"}


def summary_data() -> dict:
    rs = pd.read_csv(PROC / "routing_sensitivity.csv")
    rs = rs[rs["routing"] == "S-shape"].set_index("scenario")["change_vs_random"]
    # Next year: every layout with the new-product rule (Module 11b, year-over-year split, S-shape);
    # in-sample there are no new SKUs, so the rule changes nothing there.
    npr = pd.read_csv(PROC / "new_product_rule_routing.csv")
    npr = npr[(npr["split"] == "year-over-year") & (npr["route"] == "S-shape")].set_index("scenario")["change_vs_random"]
    walk = [(s, -100 * rs[s], -100 * npr[s + " + new-product rule"]) for s in LAYOUTS[1:]]
    a = pd.read_csv(PROC / "congestion_simulation.csv")
    a = a[(a["case"] == "base") & (a["volume"] == 10) & (a["pickers"] == 23) & (a["route"] == "S-shape")]
    b = pd.read_csv(PROC / "congestion_refined.csv")
    b = b[(b["volume"] == 10) & (b["pickers"] == 23) & (b["aisle_model"] == "narrow") & (b["policy"] == "wait")]
    b = b.drop_duplicates("scenario")
    cyc = [(s, a.set_index("scenario").loc[s, "cycle_time_min"], b.set_index("scenario").loc[s, "cycle_time_min"])
           for s in LAYOUTS]
    t = smallest_teams(pd.read_csv(PROC / "staffing_by_layout_x1.csv")).set_index(["scenario", "cutoff"])["smallest_team"]
    team = [(s, t[(s, "none")], t[(s, "18:00")]) for s in LAYOUTS]
    return {"walk": walk, "cycle": cyc, "team": team}


def _style(ax, fs):
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK, length=0, labelsize=fs)
    ax.set_yticks([])


def _bars(ax, labels, before, after, names, fmt, fs, missing="not tested"):
    x = np.arange(len(labels))
    w = 0.38
    top = np.nanmax(np.concatenate([before, after]))
    for off, vals, color, name in ((-w / 2, before, BEFORE, names[0]), (w / 2, after, AFTER, names[1])):
        ax.bar(x + off, np.nan_to_num(vals), w - 0.03, color=color, label=name)
        for xi, v in zip(x + off, vals):
            txt = missing if np.isnan(v) else fmt(v)
            ax.text(xi, (0 if np.isnan(v) else v) + top * 0.02, txt, ha="center", va="bottom", color=INK if not np.isnan(v) else INK_MUTED,
                    fontsize=fs if not np.isnan(v) else fs * 0.6, rotation=0 if not np.isnan(v) else 90)
    ax.set_xticks(x, labels)
    ax.set_ylim(0, top * 1.5)
    ax.legend(loc="upper left", frameon=False, fontsize=fs * 0.85, ncol=2, bbox_to_anchor=(0, 1.02))
    _style(ax, fs)


def plot_summary(d: dict, path: Path, size_in, dpi, fs) -> None:
    fig, axes = plt.subplots(3, 1, figsize=size_in)
    w = d["walk"]
    nxt = np.array([v for *_, v in w])
    titles = (f"1. Aisle-based storage: {nxt.max():.0f}% less walking next year",
              "2. At 10x volume, the aisle jam was a false alarm",
              "3. An 18:00 order cut-off saves 2-3 pickers today")
    subs = ("Walking saved vs. a random layout, S-shape route, new-product rule (%)",
            "Minutes from order to picked, 10x volume, 23 pickers",
            "Smallest team meeting the service level, today's volume")
    labels = [SHORT[s] for s, *_ in w]
    _bars(axes[0], labels, np.array([v for _, v, _ in w]), nxt,
          ("Same year (in-sample)", "Next year (out-of-sample)"), lambda v: f"{v:.0f}%", fs)
    c = d["cycle"]
    _bars(axes[1], [SHORT[s] for s, *_ in c], np.array([v for _, v, _ in c]), np.array([v for *_, v in c]),
          ("Strict model (whole aisle)", "Realistic (5 m sections)"), lambda v: f"{v:.0f}", fs)
    t = d["team"]
    _bars(axes[2], [SHORT[s] for s, *_ in t], np.array([v for _, v, _ in t], float), np.array([v for *_, v in t], float),
          ("No cut-off", "18:00 cut-off"), lambda v: f"{v:.0f}", fs, missing="> 8")
    for ax, ti, sub in zip(axes, titles, subs):
        ax.set_title(f"{ti}\n", loc="left", fontsize=fs * 1.15, color=INK, fontweight="bold", pad=fs * 1.6)
        ax.text(0, 1.13, sub, transform=ax.transAxes, fontsize=fs * 0.85, color=INK_MUTED, va="bottom")
    fig.text(0.01, 0.003, "Real orders: UCI Online Retail II (19,773 orders, 305 days). Warehouse, speeds and shifts are modeled.",
             fontsize=fs * 0.65, color=INK_MUTED)
    fig.tight_layout(rect=(0, 0.02, 1, 1), h_pad=fs * 0.15)
    fig.savefig(path, dpi=dpi, facecolor="white")
    plt.close(fig)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--plot-only", action="store_true")
    ap.add_argument("--out-of-sample", action="store_true", help="only the Aisle-based out-of-sample check")
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    args = ap.parse_args()
    cfgs = configs()
    if args.estimate:
        r = m8c.run_config({"volume": 1, "pickers": 4, "scenario": sr.SCENARIO_RANDOM, "policy": "wait", "cutoff_h": 18})
        print(f"  x1 Random 4 pickers 18:00: {r['runtime_s']:.1f} s single process "
              f"| SLA {r['meets_sla']} | cost/order {r['cost_units_per_order']:.3f}")
        print(f"  {len(cfgs)} configurations: ~{len(cfgs) * r['runtime_s'] * 1.9 / args.workers / 60:.1f} min "
              f"on {args.workers} processes (x1.9 parallel slowdown seen in 8c)")
        return
    if args.out_of_sample or not args.plot_only:
        t0 = time.perf_counter()
        oos = aisle_out_of_sample()
        oos.to_csv(PROC / "aisle_out_of_sample.csv", index=False)
        print("\n--- AISLE-BASED VELOCITY, OUT-OF-SAMPLE (Module 4 splits, S-shape) ---")
        print(oos.drop(columns=["avg_distance_per_order_min_m", "avg_distance_per_order_max_m"])
              .to_string(index=False, float_format=lambda v: f"{v:,.4f}"))
        print(f"({time.perf_counter() - t0:.0f} s)")
    if not args.plot_only and not args.out_of_sample:
        t0 = time.perf_counter()
        print(f"Scope: {len(cfgs)} x1 configurations on {args.workers} processes", flush=True)
        with Pool(args.workers) as pool:
            out = list(pool.imap_unordered(_run, cfgs))
        res = pd.DataFrame(out)
        res["scenario"] = pd.Categorical(res["scenario"], LAYOUTS, ordered=True)
        res = res.sort_values(["scenario", "cutoff_h", "pickers"]).reset_index(drop=True)
        res["scenario"] = res["scenario"].astype(str)
        print(f"Simulation: {(time.perf_counter() - t0) / 60:.1f} min")
        check_8b(res)
        res.to_csv(PROC / "staffing_by_layout_x1.csv", index=False)
        pd.set_option("display.width", 250)
        cols = ["scenario", "cutoff_h", "pickers", "meets_sla", "pre_cutoff_same_day_pct", "post_cutoff_on_time_pct",
                "days_overtime_ok_pct", "cost_units_per_order", "cycle_time_min", "carried_over_share_pct"]
        print("\n--- STAFFING PER LAYOUT, x1 (narrow, S-shape, wait) ---")
        print(res[cols].to_string(index=False, float_format=lambda v: f"{v:,.2f}"))
        print("\n--- SMALLEST TEAM MEETING THE SERVICE LEVEL ---")
        print(smallest_teams(res).to_string(index=False, float_format=lambda v: f"{v:,.2f}"))
    d = summary_data()
    print("\n--- SUMMARY FIGURE DATA ---")
    for k, v in d.items():
        print(k, [(s, *(round(float(x), 1) for x in r)) for s, *r in v])
    plot_summary(d, FIG / "00_summary.png", (9, 13.5), 150, 17)
    plot_summary(d, FIG / "linkedin_summary.png", (8, 10), 150, 15)


if __name__ == "__main__":
    main()
