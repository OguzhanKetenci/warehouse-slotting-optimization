"""
Module 8c - Order Cut-off and Staffing; corrected overtime for Module 8a

1. Cut-off based service level (on top of Module 8b's segment model, narrow aisles, S-shape):
   - orders arriving BEFORE the cut-off: >= 95% completed the same day (before midnight);
   - orders arriving AFTER the cut-off: completed by the next working day's shift start + 2 h
     (10:00); no night picking - post-cut-off orders are picked during the shift only when no
     other order is waiting, and those not started by 18:00 carry over to the next working day,
     where they are first in the queue;
   - overtime rule as in 8b: overtime WORK per picker <= 30 min on >= 95% of days.
   Days are no longer independent (carry-over), so each configuration simulates the year day by
   day in order. Cut-offs 16:00, 17:00, 18:00; 24:00 (no cut-off) reproduces Module 8b exactly.
2. Module 8a's overtime recomputed as overtime WORK (8b's definition) for the configurations
   shown in the 8a README table; all other 8a metrics must reproduce the stored CSV exactly.

Input : as Modules 8a / 8b
Run   : python src/08c_cutoff_service.py --estimate
        python src/08c_cutoff_service.py
Output:
  data/processed/cutoff_service.csv
  data/processed/congestion_simulation_overtime_work.csv   (8a: old and corrected overtime)
  reports/figures/11_cutoff_staffing.png
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


m8b = load_module("08b_congestion_refined.py", "congestion_refined")
m8, sr, m5 = m8b.m8, m8b.sr, m8b.m5

SHIFT_START_S, SHIFT_END_S = m8b.SHIFT_START_H * 3600, m8b.SHIFT_END_H * 3600
NEXT_DAY_DEADLINE_S = SHIFT_START_S + 2 * 3600          # 10:00 on the next working day
CUTOFFS_H = (16, 17, 18)
NO_CUTOFF_H = 24
SLA_PCT = 95.0
SCENARIOS = (sr.SCENARIO_VELOCITY, sr.SCENARIO_CLASS, m5.SCENARIO_AISLE)
POLICIES = ("wait", "skip")
PICKER_GRID = m8b.PICKER_GRID                            # same team sizes as Module 8b
INK, INK_MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"


# ----------------------------------------------------------------------------
# Year simulation with carry-over
# ----------------------------------------------------------------------------
def simulate_year(arrivals: pd.DataFrame, routes, n_pickers, capacity, policy, cutoff_h):
    """Simulate every working day in order. Returns per-order arrays over all orders (start / end
    in seconds after midnight of the day the order was picked; days_late = working days between
    arrival and picking), the post-cut-off flag, per-day overtime work and presence, and the
    number of orders still unpicked after the last day. Carried orders enter the queue at
    midnight with priority over the new day's orders, so they are picked first from 08:00."""
    n = len(arrivals)
    rec = {k: np.full(n, np.nan) for k in ("start", "end", "wait", "walk", "skips", "days_late")}
    post = (arrivals["t"].to_numpy() >= cutoff_h * 3600).astype(int)
    carry_id = np.zeros(0, dtype=int)
    carry_base = np.zeros(0, dtype=int)
    carry_age = np.zeros(0)
    ot_work, ot_presence = [], []
    for _, day in arrivals.groupby("date", sort=True):
        today = day.index.to_numpy()
        ids = np.concatenate([carry_id, today])
        base = np.concatenate([carry_base, day["base"].to_numpy()])
        t = np.concatenate([np.zeros(len(carry_id)), day["t"].to_numpy()])
        age = np.concatenate([carry_age, np.zeros(len(today))])
        deferrable = np.concatenate([np.zeros(len(carry_id), dtype=int), post[today]])
        out, last, _ = m8b.simulate_day(t, base, routes, n_pickers, capacity, policy,
                                        deferrable=deferrable, defer_at_s=SHIFT_END_S)
        done = out["deferred"] == 0
        for k in ("start", "end", "wait", "walk", "skips"):
            rec[k][ids[done]] = out[k][done]
        rec["days_late"][ids[done]] = age[done]
        ot_work.append(np.maximum(out["end"][done] - np.maximum(out["start"][done], SHIFT_END_S), 0).sum() / 3600)
        ot_presence.append(np.maximum(last - SHIFT_END_S, 0).sum() / 3600)
        carry_id, carry_base, carry_age = ids[~done], base[~done], age[~done] + 1
    return rec, post, np.array(ot_work), np.array(ot_presence), len(carry_id)


def run_config(cfg):
    t0 = time.perf_counter()
    routes = m8b.routes_for(cfg["scenario"], "S-shape")
    arr = m8b.arrivals_for(cfg["volume"]).reset_index(drop=True)
    rec, post, ot_work, ot_pres, left = simulate_year(arr, routes, cfg["pickers"],
                                                       m8b.AISLE_MODELS["narrow"], cfg["policy"], cfg["cutoff_h"])
    n, days = len(arr), len(ot_work)
    picked = ~np.isnan(rec["end"])
    same_day = picked & (rec["days_late"] == 0) & (rec["end"] < 86_400)
    next_ok = picked & (rec["days_late"] == 1) & (rec["end"] <= NEXT_DAY_DEADLINE_S)
    pre, postm = post == 0, post == 1
    pre_same_day = 100 * same_day[pre].mean()
    post_ok = 100 * (same_day | next_ok)[postm].mean() if postm.any() else 100.0
    ot_ok = 100 * np.mean(ot_work / cfg["pickers"] <= m8b.SLA_OVERTIME_PER_PICKER_H)
    cost = (cfg["pickers"] * (m8b.SHIFT_END_H - m8b.SHIFT_START_H) * days
            + m8b.OVERTIME_FACTOR * ot_work.sum()) * m8b.WAGE_PER_H
    t_arr = arr["t"].to_numpy()
    cycle = np.where(picked, rec["days_late"] * 86_400 + rec["end"] - t_arr, np.nan)  # calendar gaps ignored
    return {**{k: cfg[k] for k in ("volume", "pickers", "scenario", "policy", "cutoff_h")},
            "n_orders": n, "days": days,
            "post_cutoff_share_pct": 100 * postm.mean(),
            "carried_over_share_pct": 100 * np.mean(picked & (rec["days_late"] >= 1)),
            "carried_over_of_post_pct": 100 * np.mean((picked & (rec["days_late"] >= 1))[postm]) if postm.any() else 0.0,
            "pre_cutoff_same_day_pct": pre_same_day,
            "post_cutoff_on_time_pct": post_ok,
            "days_overtime_ok_pct": ot_ok,
            "meets_sla": bool(pre_same_day >= SLA_PCT and post_ok >= SLA_PCT and ot_ok >= SLA_PCT),
            "overtime_work_picker_h_per_day": ot_work.mean(),
            "overtime_presence_picker_h_per_day": ot_pres.mean(),
            "cost_units_per_day": cost / days, "cost_units_per_order": cost / n,
            "aisle_wait_per_order_s": np.nanmean(rec["wait"]),
            "cycle_time_min": np.nanmean(cycle) / 60,
            "orders_unpicked_at_end": left,
            "runtime_s": time.perf_counter() - t0}


# ----------------------------------------------------------------------------
# Module 8a overtime, recomputed as work
# ----------------------------------------------------------------------------
M8A_TABLE = [(1, 4), (5, 12), (10, 23)]                  # (volume, pickers) in the 8a README table


def m8a_overtime(cfg):
    """Re-simulate one 8a configuration with Module 8a's own code and return old (presence) and
    new (work) overtime, plus the stored metrics it must reproduce."""
    routes = m8a_routes(cfg["scenario"], cfg["route"])
    arr = m8.replicate(m8.load_orders()[4], cfg["volume"])
    pres, work, waits = [], [], []
    for _, day in arr.groupby("date", sort=True):
        s, e, w, last, _ = m8.simulate_day(day["t"].to_numpy(), day["base"].to_numpy(), routes,
                                           cfg["pickers"], m8.AISLE_CAPACITY)
        pres.append(np.maximum(last - SHIFT_END_S, 0).sum() / 3600)
        work.append(np.maximum(e - np.maximum(s, SHIFT_END_S), 0).sum() / 3600)
        waits.append(w)
    return {**cfg, "overtime_presence_picker_h_per_day": np.mean(pres),
            "overtime_work_picker_h_per_day": np.mean(work),
            "aisle_wait_per_order_s": np.concatenate(waits).mean()}


_M8A = {}


def m8a_routes(scenario, route):
    key = (scenario, route)
    if key not in _M8A:
        _M8A.clear()
        layout, sku, visit_order, visit_sku, orders = m8.load_orders()
        ranks = dict((s, r) for s, seed, r in m8.m7.layouts(sku, layout, m5, 1) if seed in (None, 0))
        if route == "S-shape":
            a, y = m8.m7.pick_coordinates(ranks[scenario], layout, visit_sku)
            rr = [m8.s_shape_route(oa, oy) for oa, oy in m8.m7.split_orders(visit_order, a, y)]
        else:
            tours = pd.read_pickle(m8.TOUR_CACHE)["tours"]
            rr = [m8.tour_route(r[1], r[2], r[3], r[4]) for r in tours[scenario]]
        _M8A[key] = m8.timed_routes(rr, m8.PICK_TIME_S)
    return _M8A[key]


# ----------------------------------------------------------------------------
# Scope ("minimum", chosen by the user after the ~3 h estimate for the full brief)
# ----------------------------------------------------------------------------
SCENARIO, POLICY = sr.SCENARIO_VELOCITY, "wait"
# x1: Module 8b's grid. x5 / x10: shifted above 8b's grids, where 8b never met its service level.
TEAMS = {1: (3, 4, 5, 6, 8), 5: (14, 18, 24), 10: (28, 36, 44)}
CHECK_8B = {"volume": 1, "pickers": 4, "scenario": SCENARIO, "policy": POLICY, "cutoff_h": NO_CUTOFF_H}
SPEED_BASED_8B = {1: "6", 5: ">= 20", 10: ">= 40"}       # Module 8b, Full velocity, narrow, wait
M8A_SCENARIOS = (sr.SCENARIO_RANDOM, sr.SCENARIO_CLASS, sr.SCENARIO_VELOCITY, m5.SCENARIO_AISLE)
EST_S = {1: 35, 5: 168, 10: 330}                         # single-process seconds per cut-off configuration
EST_8A_S = {1: 10, 5: 40, 10: 75}


def _dispatch(cfg):
    if cfg["kind"] == "cutoff":
        return {"kind": "cutoff", **run_config(cfg)}
    return {"kind": "8a", **{k: v for k, v in m8a_overtime(cfg).items() if k != "kind"}}


def check_8b(cut: pd.DataFrame) -> None:
    """Cut-off 24:00 (nothing deferred) must reproduce Module 8b's stored row exactly."""
    mine = cut[cut["cutoff_h"] == NO_CUTOFF_H].iloc[0]
    ref = pd.read_csv(PROC / "congestion_refined.csv")
    ref = ref[(ref["volume"] == CHECK_8B["volume"]) & (ref["pickers"] == CHECK_8B["pickers"])
              & (ref["scenario"] == SCENARIO) & (ref["aisle_model"] == "narrow") & (ref["policy"] == POLICY)
              & ref["set"].str.contains("team_size")].iloc[0]
    pairs = [("aisle_wait_per_order_s", "aisle_wait_per_order_s"), ("cycle_time_min", "cycle_time_min"),
             ("overtime_work_picker_h_per_day", "overtime_work_picker_h_per_day"),
             ("overtime_presence_picker_h_per_day", "overtime_picker_h_per_day"),
             ("pre_cutoff_same_day_pct", "orders_same_day_pct"), ("days_overtime_ok_pct", "days_overtime_ok_pct"),
             ("cost_units_per_order", "cost_units_per_order")]
    diff = max(abs(mine[a] - ref[b]) for a, b in pairs)
    if diff > 1e-9 or mine["carried_over_share_pct"] != 0:
        raise AssertionError(f"cut-off 24:00 does not reproduce Module 8b (max |diff| {diff})")
    print(f"Check OK: cut-off 24:00 reproduces Module 8b's x{CHECK_8B['volume']} / {CHECK_8B['pickers']} pickers "
          f"row on {len(pairs)} metrics (max |diff| {diff:.1e}), nothing carried over.")


def check_and_merge_8a(m8a: pd.DataFrame) -> pd.DataFrame:
    """The re-simulated 8a configurations must reproduce the stored aisle wait and (presence)
    overtime exactly; returns old and corrected overtime side by side."""
    ref = pd.read_csv(PROC / "congestion_simulation.csv")
    ref = ref[ref["case"] == "base"]
    merged = m8a.merge(ref[["volume", "pickers", "scenario", "route", "aisle_wait_per_order_s", "overtime_picker_h_per_day"]],
                       on=["volume", "pickers", "scenario", "route"], suffixes=("", "_stored"))
    assert len(merged) == len(m8a)
    diff = max(float((merged["aisle_wait_per_order_s"] - merged["aisle_wait_per_order_s_stored"]).abs().max()),
               float((merged["overtime_presence_picker_h_per_day"] - merged["overtime_picker_h_per_day"]).abs().max()))
    if diff > 1e-9:
        raise AssertionError(f"8a re-simulation differs from the stored results (max |diff| {diff})")
    print(f"Check OK: {len(merged)} re-simulated 8a configurations reproduce the stored aisle wait and overtime "
          f"exactly (max |diff| {diff:.1e}).")
    out = merged[["volume", "pickers", "scenario", "route", "overtime_picker_h_per_day", "overtime_work_picker_h_per_day"]]
    out = out.rename(columns={"overtime_picker_h_per_day": "overtime_old_presence_picker_h_per_day",
                              "overtime_work_picker_h_per_day": "overtime_new_work_picker_h_per_day"})
    return out.sort_values(["volume", "route", "scenario"]).reset_index(drop=True)


def team_table(cut: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (v, c), g in cut.groupby(["volume", "cutoff_h"]):
        g = g.sort_values("pickers").reset_index(drop=True)
        ok = np.flatnonzero(g["meets_sla"].to_numpy())
        k = int(ok[0]) if len(ok) else None
        below = g.loc[k - 1, "pickers"] if (k is not None and k > 0) else None
        rows.append({"volume": v, "cutoff": f"{c}:00",
                     "smallest_team_meeting_sla": (f"{g.loc[k, 'pickers']}" + (f" (fails at {below})" if below else
                                                    " (lowest tested)")) if k is not None else f"> {g['pickers'].max()}",
                     "cost_units_per_order": g.loc[k, "cost_units_per_order"] if k is not None else np.nan,
                     "cycle_time_min": g.loc[k, "cycle_time_min"] if k is not None else np.nan,
                     "carried_over_share_pct": g.loc[k, "carried_over_share_pct"] if k is not None else np.nan,
                     "post_cutoff_share_pct": g["post_cutoff_share_pct"].iloc[0],
                     "speed_based_8b": SPEED_BASED_8B[v]})
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# Figure
# ----------------------------------------------------------------------------
CUTOFF_COLORS = {16: "#2a78d6", 17: "#eb6834", 18: "#1baf7a"}


def plot_cutoff(cut: pd.DataFrame, path: Path) -> None:
    vols = sorted(cut["volume"].unique())
    fig, axes = plt.subplots(2, len(vols), figsize=(14, 7.4), sharey="row")
    cut = cut.assign(worst=cut[["pre_cutoff_same_day_pct", "post_cutoff_on_time_pct", "days_overtime_ok_pct"]].min(axis=1))
    for col, v in enumerate(vols):
        for row, (metric, ylab) in enumerate((("worst", "Worst of the three service criteria (%)"),
                                              ("carried_over_share_pct", "Orders carried to the next day (%)"))):
            ax = axes[row, col]
            for c in CUTOFFS_H:
                g = cut[(cut["volume"] == v) & (cut["cutoff_h"] == c)].sort_values("pickers")
                ax.plot(g["pickers"], g[metric], color=CUTOFF_COLORS[c], lw=2, marker="o", ms=5, label=f"Cut-off {c}:00")
                if row == 0:
                    met = g[g["meets_sla"]]
                    ax.scatter(met["pickers"], met[metric], s=90, facecolors="none", edgecolors=CUTOFF_COLORS[c], lw=1.6, zorder=3)
            if row == 0:
                ax.axhline(SLA_PCT, color=INK_MUTED, lw=1, ls="--")
                ax.set_title(f"x{v} volume", color=INK, loc="left", fontsize=11)
            ax.set_xticks(TEAMS[v])
            if col == 0:
                ax.set_ylabel(ylab, color=INK_MUTED)
            if row == 1:
                ax.set_xlabel("Number of pickers", color=INK_MUTED)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
            for side in ("left", "bottom"):
                ax.spines[side].set_color(GRID)
            ax.tick_params(colors=INK_MUTED, length=0)
            ax.yaxis.grid(True, color=GRID, lw=0.8)
            ax.set_axisbelow(True)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=3, frameon=False, fontsize=10, bbox_to_anchor=(0.5, 1.0))
    fig.suptitle("Order cut-off and staffing: service level and next-day carry-over (Full velocity, narrow aisles, S-shape)",
                 x=0.01, y=1.045, ha="left", fontsize=12, color=INK)
    fig.text(0.01, -0.03, "Service criteria: pre-cut-off orders done the same day; post-cut-off orders done by 10:00 the next "
                          "working day; overtime work per picker <= 30 min. Dashed line = 95%; circled points meet all three.",
             fontsize=8, color=INK_MUTED)
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--plot-only", action="store_true")
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    args = ap.parse_args()
    if args.estimate:
        for v, n in ((1, 4), (5, 14)):
            c = {"volume": v, "pickers": n, "scenario": sr.SCENARIO_VELOCITY, "policy": "wait", "cutoff_h": 17}
            r = run_config(c)
            print(f"  cut-off x{v} {n}: {r['runtime_s']:.1f} s | carried {r['carried_over_share_pct']:.2f}% "
                  f"| pre same-day {r['pre_cutoff_same_day_pct']:.2f}% | post on time {r['post_cutoff_on_time_pct']:.2f}% "
                  f"| OT-ok days {r['days_overtime_ok_pct']:.1f}%", flush=True)
        t = time.perf_counter()
        m8a_overtime({"volume": 1, "pickers": 4, "scenario": sr.SCENARIO_VELOCITY, "route": "S-shape"})
        print(f"  8a re-simulation x1 4: {time.perf_counter() - t:.1f} s")
        return
    if args.plot_only:
        plot_cutoff(pd.read_csv(PROC / "cutoff_service.csv"), FIG / "11_cutoff_staffing.png")
        return

    t0 = time.perf_counter()
    cut_cfgs = [{"kind": "cutoff", "volume": v, "pickers": n, "scenario": SCENARIO, "policy": POLICY, "cutoff_h": c}
                for c in CUTOFFS_H for v in TEAMS for n in TEAMS[v]]
    check_cfg = {"kind": "cutoff", **CHECK_8B}
    m8a_cfgs = [{"kind": "8a", "volume": v, "pickers": n, "scenario": s, "route": r}
                for s in M8A_SCENARIOS for r in ("S-shape", "Optimal") for v, n in M8A_TABLE]
    tasks = sorted(cut_cfgs + [check_cfg], key=lambda c: -EST_S[c["volume"]]) + m8a_cfgs
    est = sum(EST_S[c["volume"]] for c in cut_cfgs) + sum(EST_8A_S[c["volume"]] for c in m8a_cfgs)
    print(f"Scope: {len(cut_cfgs)} cut-off configurations + 1 check (24:00) + {len(m8a_cfgs)} Module 8a "
          f"re-simulations; estimated {est / 60 / 4.5:.0f} min on {args.workers} processes", flush=True)
    with Pool(args.workers) as pool:
        out = []
        for r in pool.imap_unordered(_dispatch, tasks):
            out.append(r)
            if len(out) % 6 == 0 or len(out) == len(tasks):
                print(f"  done {len(out):>2}/{len(tasks)} ({(time.perf_counter() - t0) / 60:4.1f} min)", flush=True)
    cut = pd.DataFrame([r for r in out if r["kind"] == "cutoff"]).drop(columns="kind")
    m8a = pd.DataFrame([r for r in out if r["kind"] == "8a"]).drop(columns="kind")
    print(f"Simulation: {(time.perf_counter() - t0) / 60:.1f} min")

    check_8b(cut)
    m8a = check_and_merge_8a(m8a)
    cut = cut[cut["cutoff_h"] != NO_CUTOFF_H].sort_values(["volume", "cutoff_h", "pickers"]).reset_index(drop=True)
    cut.to_csv(PROC / "cutoff_service.csv", index=False)
    m8a.to_csv(PROC / "congestion_simulation_overtime_work.csv", index=False)

    pd.set_option("display.width", 250)
    fmt = lambda v: f"{v:,.2f}"
    print("\n--- CUT-OFF RESULTS (Full velocity, narrow, S-shape, wait) ---")
    print(cut.drop(columns=["runtime_s", "scenario", "policy"]).to_string(index=False, float_format=fmt))
    teams = team_table(cut)
    print("\n--- SMALLEST TEAM MEETING THE CUT-OFF SERVICE LEVEL ---")
    print(teams.to_string(index=False, float_format=fmt))
    print("\n--- MODULE 8a OVERTIME: OLD (presence, wrong definition) VS. NEW (work) ---")
    print(m8a.to_string(index=False, float_format=fmt))
    plot_cutoff(cut, FIG / "11_cutoff_staffing.png")
    print(f"\nTotal runtime: {(time.perf_counter() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
