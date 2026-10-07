"""
Module 11b - New-product rule for every layout, out-of-sample, under every routing rule

SKUs never picked in the ranking (history) period are moved to the rank position where the
Class-based ABC B zone starts (= number of A-class SKUs in the history period):
  - Class-based ABC: Module 6's P7 rule (hybrid_ranks, N=0, new_sku_rule="start_of_b").
  - Full velocity: seen SKUs keep their velocity order; the new SKUs are inserted as one block
    at that position (ties by SKU code, as in Module 2).
  - Aisle-based velocity: the same order, mapped onto Module 5's aisle-filling slot order.
Without the rule each function reproduces the original layout exactly (asserted).

Grid: Random, Class-based ABC, Full velocity, Aisle-based velocity (each non-random layout with
and without the rule) x S-shape, Return, Largest gap, Combined, Optimal x Module 4's two splits.
Everything else as Modules 4 and 7 (layout, visits, random baseline, OR-Tools settings).

Run   : python src/11b_new_product_rule.py --estimate
        python src/11b_new_product_rule.py [--optimal-splits year-over-year] [--optimal-seeds 3]
Output: data/processed/new_product_rule_routing.csv
"""
import argparse
import importlib.util
import os
import sys
import time
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"


def load_module(filename: str, name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "src" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module                          # so Pool workers can unpickle its functions
    spec.loader.exec_module(module)
    return module


sr = load_module("02_slotting_routing.py", "slotting_routing")
m5 = load_module("05_routing_sensitivity.py", "routing_sensitivity")
m7 = load_module("07_optimal_routing.py", "optimal_routing")

ROUTES = ("S-shape", "Return", "Largest gap", "Combined", "Optimal")
RULE_SUFFIX = " + new-product rule"


def velocity_order(pick: np.ndarray, n_a: int, rule: bool) -> np.ndarray:
    """SKU indices from nearest to farthest rank. Without the rule: Module 2's stable velocity
    order (new SKUs, pick = 0, last). With it: new SKUs moved as one block to position n_a."""
    order = np.argsort(-pick, kind="stable")
    if not rule:
        return order
    new = pick[order] == 0
    seen = order[~new]
    return np.concatenate([seen[:n_a], order[new], seen[n_a:]])


def full_velocity(pick, n_a, rule):
    ranks = np.empty(len(pick), dtype=int)
    ranks[velocity_order(pick, n_a, rule)] = np.arange(len(pick))
    return ranks


def aisle_based(pick, n_a, rule, layout):
    aisle_major = layout.sort_values(["aisle", "pos", "side"], kind="mergesort").index.to_numpy()
    ranks = np.empty(len(pick), dtype=int)
    ranks[velocity_order(pick, n_a, rule)] = aisle_major[: len(pick)]
    return ranks


def layouts(pick, cls, layout, m6, n_seeds):
    """(scenario, seed, ranks) for every layout of the grid, out-of-sample (history ranking)."""
    n, n_slots = len(pick), len(layout)
    n_a = int((cls == "A").sum())
    seeds = sr.RANDOM_SEEDS[:n_seeds]
    for s in seeds:
        yield sr.SCENARIO_RANDOM, s, sr.slot_random(n, n_slots, np.random.default_rng(s))
    for s in seeds:
        yield sr.SCENARIO_CLASS, s, sr.slot_class_based(cls, np.random.default_rng(s))
        yield sr.SCENARIO_CLASS + RULE_SUFFIX, s, m6.hybrid_ranks(sr, pick, cls, 0, np.random.default_rng(s), "start_of_b")
    for rule in (False, True):
        sfx = RULE_SUFFIX if rule else ""
        yield sr.SCENARIO_VELOCITY + sfx, None, full_velocity(pick, n_a, rule)
        yield m5.SCENARIO_AISLE + sfx, None, aisle_based(pick, n_a, rule, layout)


def check_rules(pick, cls, layout, m6):
    assert np.array_equal(full_velocity(pick, 0, False), sr.slot_full_velocity(pick))
    assert np.array_equal(aisle_based(pick, 0, False, layout), m5.slot_aisle_based_velocity(pick, layout))
    rng = lambda: np.random.default_rng(0)
    assert np.array_equal(m6.hybrid_ranks(sr, pick, cls, 0, rng(), "end"), sr.slot_class_based(cls, rng()))
    n_a = int((cls == "A").sum())
    new = np.flatnonzero(pick == 0)
    for name, r in (("FV", full_velocity(pick, n_a, True)), ("ABC", m6.hybrid_ranks(sr, pick, cls, 0, rng(), "start_of_b"))):
        assert sorted(r[new].tolist()) == list(range(n_a, n_a + len(new))), name   # new SKUs right after A
    return n_a, len(new)


def splits(m4, m1):
    h6, f6, _ = m4.within_year_periods()
    codes6 = sorted(pd.read_csv(PROC / "sku_velocity.csv", dtype={"StockCode": str})["StockCode"])
    hy, fy, _ = m4.year_over_year_periods(m1)
    return [(m4.SPLIT_YOY, hy, fy, sorted(fy["StockCode"].unique())), (m4.SPLIT_6_6, h6, f6, codes6)]


def prepare(m4, m1, hist, fut, codes):
    layout, n_aisles = m4.size_layout(sr, len(codes))
    assert n_aisles == sr.N_AISLES                      # Module 7's graph assumes the default 40 aisles
    idx = pd.Series(np.arange(len(codes)), index=codes)
    vo, vs = m4.order_visits(fut, idx)
    pick, cls = m4.sku_stats(hist, codes, m1.abc_class)
    return layout, vo, vs, pick, cls


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    ap.add_argument("--seeds", type=int, default=len(sr.RANDOM_SEEDS), help="seeds for the four rules")
    ap.add_argument("--optimal-seeds", type=int, default=len(sr.RANDOM_SEEDS))
    ap.add_argument("--optimal-splits", default="year-over-year,6+6 months")
    args = ap.parse_args()
    m4 = load_module("04_out_of_sample_check.py", "out_of_sample_check")
    m1 = load_module("01_order_profile_abc.py", "order_profile_abc")
    m6 = load_module("06_reslotting_policies.py", "reslotting_policies")
    t0 = time.perf_counter()
    data = [(name, *prepare(m4, m1, h, f, c)) for name, h, f, c in splits(m4, m1)]
    print(f"Data loaded: {time.perf_counter() - t0:.0f} s", flush=True)
    for name, layout, vo, vs, pick, cls in data:
        n_a, n_new = check_rules(pick, cls, layout, m6)
        print(f"Check OK ({name}): rules reproduce the original layouts; {n_new} new SKUs placed at ranks "
              f"{n_a}-{n_a + n_new - 1} (B zone starts at {n_a})", flush=True)

    if args.estimate:
        rng = np.random.default_rng(0)
        name, layout, vo, vs, pick, cls = data[0]
        n_orders = int(vo.max()) + 1
        chosen = np.sort(rng.choice(n_orders, 1000, replace=False))
        keep = np.isin(vo, chosen)
        svo, svs = pd.factorize(vo[keep])[0], vs[keep]
        times = {}
        with Pool(args.workers) as pool:
            for scen, seed, ranks in layouts(pick, cls, layout, m6, 1):
                a, y = m7.pick_coordinates(ranks, layout, svs)
                t = time.perf_counter()
                m7.optimal_distances(svo, a, y, pool, m7.GLS_SOLUTIONS, m7.GLS_MAX_NODES)
                times[scen] = (time.perf_counter() - t) / 1000
                print(f"  Optimal, 1,000 orders, {scen}: {times[scen] * 1000:.1f} s", flush=True)
            t = time.perf_counter()
            a, y = m7.pick_coordinates(next(layouts(pick, cls, layout, m6, 1))[2], layout, vs)
            for fn in (sr.s_shape_distances, sr.return_distances, sr.largest_gap_distances, m7.combined_distances):
                fn(vo, a, y)
            rules_s = time.perf_counter() - t
        orders = {nm: int(v.max()) + 1 for nm, _, v, *_ in data}
        rand_like = np.mean([times[sr.SCENARIO_RANDOM], times[sr.SCENARIO_CLASS], times[sr.SCENARIO_CLASS + RULE_SUFFIX]])
        det = sum(v for k, v in times.items() if k not in (sr.SCENARIO_RANDOM, sr.SCENARIO_CLASS, sr.SCENARIO_CLASS + RULE_SUFFIX))
        print(f"  4 rules on one layout, all {orders[data[0][0]]:,} orders: {rules_s:.1f} s")
        for seeds in (10, 3):
            for sp in (list(orders), [data[0][0]]):
                opt = sum(orders[s] * (3 * seeds * rand_like + det) for s in sp) / 60
                print(f"  Optimal, {seeds} seeds, splits {sp}: ~{opt:.0f} min")
        print(f"  Rules, 10 seeds, both splits: ~{sum((3 * 10 + 4) * rules_s * orders[s] / orders[data[0][0]] for s in orders) / 60:.0f} min")
        return

    # 1. The four rules on every layout and split (fast), then the check against Modules 4 / 11
    rows = []
    for name, layout, vo, vs, pick, cls in data:
        for scen, seed, ranks in layouts(pick, cls, layout, m6, args.seeds):
            a, y = m7.pick_coordinates(ranks, layout, vs)
            for route, d in m7.heuristic_distances(vo, a, y).items():
                rows.append({"split": name, "scenario": scen, "seed": seed, "route": route,
                             "avg_distance_per_order_m": d.mean(), "total_distance_km": d.sum() / 1000})
    rules = pd.DataFrame(rows)
    print(f"Four rules done: {(time.perf_counter() - t0) / 60:.1f} min", flush=True)
    check_s_shape(summarize(rules))

    # 2. Optimal, checkpointed per layout so an interrupted run can resume
    ck = PROC / "new_product_rule_optimal_checkpoint.csv"
    done = pd.read_csv(ck) if ck.exists() else pd.DataFrame(columns=["split", "scenario", "seed"])
    keys = {(r.split, r.scenario, None if pd.isna(r.seed) else int(r.seed)) for r in done.itertuples()}
    opt_splits = [s.strip() for s in args.optimal_splits.split(",")]
    with Pool(args.workers) as pool:
        for name, layout, vo, vs, pick, cls in data:
            if name not in opt_splits:
                continue
            for scen, seed, ranks in layouts(pick, cls, layout, m6, args.optimal_seeds):
                if (name, scen, seed) in keys:
                    continue
                t = time.perf_counter()
                a, y = m7.pick_coordinates(ranks, layout, vs)
                opt, _, _ = m7.optimal_distances(vo, a, y, pool, m7.GLS_SOLUTIONS, m7.GLS_MAX_NODES)
                heur = m7.heuristic_distances(vo, a, y)
                worse = int(sum((opt > h + 1e-6).sum() for h in heur.values()))   # optimal must never exceed a rule
                row = pd.DataFrame([{"split": name, "scenario": scen, "seed": seed, "route": "Optimal",
                                     "avg_distance_per_order_m": opt.mean(), "total_distance_km": opt.sum() / 1000,
                                     "orders_optimal_longer_than_a_rule": worse}])
                row.to_csv(ck, mode="a", header=not ck.exists(), index=False)
                print(f"  {name:<15} {scen:<42} seed={str(seed):<4} {time.perf_counter() - t:6.1f} s | "
                      f"optimal {opt.mean():6.1f} m | orders longer than a rule: {worse} | "
                      f"total {(time.perf_counter() - t0) / 60:.1f} min", flush=True)
    optimal = pd.read_csv(ck) if ck.exists() else pd.DataFrame(columns=list(rules.columns) + ["orders_optimal_longer_than_a_rule"])
    if (optimal["orders_optimal_longer_than_a_rule"] > 0).any():
        print("WARNING: some optimal routes are longer than a simple rule", flush=True)
    runs = pd.concat([rules, optimal.drop(columns="orders_optimal_longer_than_a_rule")], ignore_index=True)
    res = summarize(runs)
    res.to_csv(PROC / "new_product_rule_routing.csv", index=False)
    md = tables_markdown(res)
    (PROC / "new_product_rule_tables.md").write_text(md, encoding="utf-8")
    print("\n" + md)
    print(f"Total runtime: {(time.perf_counter() - t0) / 60:.1f} min", flush=True)


SCEN_ORDER = [sr.SCENARIO_CLASS, sr.SCENARIO_CLASS + RULE_SUFFIX, sr.SCENARIO_VELOCITY,
              sr.SCENARIO_VELOCITY + RULE_SUFFIX, m5.SCENARIO_AISLE, m5.SCENARIO_AISLE + RULE_SUFFIX]


def summarize(runs: pd.DataFrame) -> pd.DataFrame:
    """Mean over seeds per split x route x scenario; change vs. Random = mean total / Random's mean total - 1
    (Module 4's definition)."""
    g = runs.groupby(["split", "route", "scenario"], as_index=False).agg(
        n_runs=("total_distance_km", "size"), avg_distance_per_order_m=("avg_distance_per_order_m", "mean"),
        total_distance_km=("total_distance_km", "mean"))
    base = g[g["scenario"] == sr.SCENARIO_RANDOM].set_index(["split", "route"])["total_distance_km"]
    g["change_vs_random"] = g["total_distance_km"] / base.reindex(pd.MultiIndex.from_frame(g[["split", "route"]])).to_numpy() - 1
    return g


def check_s_shape(res: pd.DataFrame) -> None:
    """S-shape, no rule, must reproduce Module 4 (ABC, Full velocity) and Module 11 (Aisle-based)."""
    ref = pd.read_csv(PROC / "out_of_sample_results.csv")
    ref = ref[(ref["visit_scope"] == "all_visits") & (ref["ranking_basis"] == "out_of_sample")]
    ref = pd.concat([ref, pd.read_csv(PROC / "aisle_out_of_sample.csv").query(
        "visit_scope == 'all_visits' and ranking_basis == 'out_of_sample'")])
    mine = res[res["route"] == "S-shape"].merge(ref[["split", "scenario", "change_vs_random", "avg_distance_per_order_m"]],
                                                 on=["split", "scenario"], suffixes=("", "_ref"))
    diff = max(float((mine[c] - mine[f"{c}_ref"]).abs().max()) for c in ("change_vs_random", "avg_distance_per_order_m"))
    status = "OK" if len(mine) == 6 and diff < 1e-9 else "MISMATCH"
    print(f"Check {status}: S-shape without the rule vs. Modules 4 and 11, {len(mine)} rows, max |diff| {diff:.1e}", flush=True)


def tables_markdown(res: pd.DataFrame) -> str:
    out = []
    for split in ("year-over-year", "6+6 months"):
        sub = res[res["split"] == split]
        routes = [r for r in ROUTES if r in set(sub["route"])]
        pv = sub.pivot(index="scenario", columns="route", values="change_vs_random").reindex(SCEN_ORDER)[routes]
        dist = sub.pivot(index="scenario", columns="route", values="avg_distance_per_order_m")
        nr = sub.pivot(index="scenario", columns="route", values="n_runs")
        best = pv.idxmin()
        out.append(f"**{split}** (saving vs. Random, out-of-sample; Random's distance per order in the last row)\n")
        out.append("| Layout | " + " | ".join(routes) + " |")
        out.append("|---" * (len(routes) + 1) + "|")
        for scen in SCEN_ORDER:
            cells = [(f"**{-100 * pv.loc[scen, r]:.1f}%**" if best[r] == scen else f"{-100 * pv.loc[scen, r]:.1f}%") for r in routes]
            out.append(f"| {scen} | " + " | ".join(cells) + " |")
        out.append("| Random (m per order) | " + " | ".join(f"{dist.loc[sr.SCENARIO_RANDOM, r]:,.0f} m" for r in routes) + " |")
        out.append("\nSeeds (Random and Class-based ABC): " + ", ".join(f"{r} {int(nr.loc[sr.SCENARIO_RANDOM, r])}" for r in routes) + "\n")
    return "\n".join(out)


if __name__ == "__main__":
    main()
