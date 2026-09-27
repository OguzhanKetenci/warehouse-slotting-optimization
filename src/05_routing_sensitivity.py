"""
Module 5 - Routing Sensitivity: Return and Largest-Gap policies, aisle-based velocity slotting

Question: is the ranking of slotting scenarios (random worse than class-based ABC worse than full
velocity) robust to the choice of picking-route heuristic, or is it an artefact of assuming
S-shape routing? A second question: does a simpler, purely aisle-by-aisle velocity rule (no
whole-warehouse distance ranking) come close to full velocity?

4 slotting scenarios, evaluated under 3 routing policies each (same layout as Module 2, all
orders of the last 12 months):
  - Random (baseline), Class-based ABC, Full velocity: identical definitions and seeds as
    Module 2 - only the routing policy used to evaluate them changes here.
  - Aisle-based velocity (new): SKUs are ranked by pick frequency (as in Full velocity); the
    fastest SKUs fill the nearest aisle completely (both racks, 100 slots) before moving on to
    the next aisle; within an aisle, faster SKUs take the nearer positions. Deterministic.
  - S-shape (Module 2): every aisle with a pick is traversed end to end.
  - Return (this module, src/02_slotting_routing.py): every aisle with a pick is entered from
    the front cross aisle and left the way it came - never traversed end to end.
  - Largest gap (this module, src/02_slotting_routing.py): the first and last non-empty aisle
    are traversed end to end; every aisle strictly between them is entered from both the front
    and back cross aisle, skipping its single largest unpicked gap.

As a consistency check, the S-shape column for Random / Class-based ABC / Full velocity must
reproduce Module 2's scenario_results.csv exactly (same data, seeds, layout and routing formula).

Input : data/processed/clean_lines.csv, data/processed/sku_velocity.csv, data/processed/scenario_results.csv
Run   : python src/05_routing_sensitivity.py   (from repo root, after Modules 1 and 2)
Output:
  data/processed/routing_sensitivity.csv
  reports/figures/06_routing_sensitivity.png
"""
import importlib.util
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"
FIG = ROOT / "reports" / "figures"

SCENARIO_AISLE = "Aisle-based velocity"
AISLE_COLOR = "#eda100"   # categorical slot 4 of the reference palette; grouped bars validate all 4 adjacent slots
ROUTING_ORDER = ("S-shape", "Return", "Largest gap")
INK, INK_MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"


def load_module(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "src" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ----------------------------------------------------------------------------
# 4th slotting scenario: aisle-based velocity
# ----------------------------------------------------------------------------
def slot_aisle_based_velocity(pick_lines: np.ndarray, layout: pd.DataFrame) -> np.ndarray:
    """Fastest SKUs fill the nearest aisle completely (both racks), then the next aisle, and so
    on; within an aisle, faster SKUs take the nearer positions. Deterministic (ties by SKU code,
    via the stable sort on pick_lines, matching Module 2's slot_full_velocity).

    Unlike Module 2's `slot_full_velocity` (which ranks ALL slots of the warehouse by straight-
    line walking distance from the depot), this ranks slots aisle-by-aisle: an aisle's farthest
    position is always filled before the next aisle's nearest position, even though the next
    aisle's near positions may be physically closer to the depot.
    """
    aisle_major_rank = layout.sort_values(["aisle", "pos", "side"], kind="mergesort").index.to_numpy()
    order = np.argsort(-pick_lines, kind="stable")
    ranks = np.empty(len(pick_lines), dtype=int)
    ranks[order] = aisle_major_rank[: len(pick_lines)]
    return ranks


# ----------------------------------------------------------------------------
# Evaluation: 4 scenarios x 3 routing policies
# ----------------------------------------------------------------------------
def run_all(sr, sku, layout, visit_order, visit_sku, near_cut):
    routing_fns = {"S-shape": sr.s_shape_distances, "Return": sr.return_distances,
                   "Largest gap": sr.largest_gap_distances}
    sku_class = sku["abc_pick"].to_numpy()
    pick_lines = sku["pick_lines"].to_numpy()
    n_skus, n_slots = len(sku), len(layout)

    rows = []
    def record(routing_name, scenario, seed, ranks):
        avg, total_km, near = sr.evaluate(ranks, layout, visit_order, visit_sku, near_cut,
                                          distance_fn=routing_fns[routing_name])
        rows.append({"routing": routing_name, "scenario": scenario, "seed": seed,
                    "avg_distance_per_order_m": avg, "total_distance_km": total_km,
                    "share_picks_nearest_20pct_slots": near})

    for routing_name in ROUTING_ORDER:
        for seed in sr.RANDOM_SEEDS:
            record(routing_name, sr.SCENARIO_RANDOM, seed,
                  sr.slot_random(n_skus, n_slots, np.random.default_rng(seed)))
        for seed in sr.RANDOM_SEEDS:
            record(routing_name, sr.SCENARIO_CLASS, seed,
                  sr.slot_class_based(sku_class, np.random.default_rng(seed)))
        record(routing_name, sr.SCENARIO_VELOCITY, None, sr.slot_full_velocity(pick_lines))
        record(routing_name, SCENARIO_AISLE, None, slot_aisle_based_velocity(pick_lines, layout))

    runs = pd.DataFrame(rows)
    runs["seed"] = runs["seed"].astype("Int64")
    return runs


def summarize(runs: pd.DataFrame, random_name: str, scenario_order: list) -> pd.DataFrame:
    parts = []
    for routing_name in ROUTING_ORDER:
        sub = runs[runs["routing"] == routing_name]
        base = sub.loc[sub["scenario"] == random_name, "total_distance_km"].mean()
        sub = sub.assign(change=sub["total_distance_km"] / base - 1)
        g = sub.groupby("scenario", sort=False)
        part = pd.DataFrame({
            "n_runs": g["total_distance_km"].size(),
            "avg_distance_per_order_m": g["avg_distance_per_order_m"].mean(),
            "avg_distance_per_order_min_m": g["avg_distance_per_order_m"].min(),
            "avg_distance_per_order_max_m": g["avg_distance_per_order_m"].max(),
            "total_distance_km": g["total_distance_km"].mean(),
            "share_picks_nearest_20pct_slots": g["share_picks_nearest_20pct_slots"].mean(),
            "change_vs_random": g["total_distance_km"].mean() / base - 1,
            "change_vs_random_min": g["change"].min(),
            "change_vs_random_max": g["change"].max(),
        }).reset_index()
        part.insert(0, "routing", routing_name)
        parts.append(part)
    out = pd.concat(parts, ignore_index=True)
    key = out.apply(lambda r: (ROUTING_ORDER.index(r["routing"]), scenario_order.index(r["scenario"])), axis=1)
    return out.assign(_k=key).sort_values("_k").drop(columns="_k").reset_index(drop=True)


def sanity_check_against_module2(sr, results: pd.DataFrame) -> None:
    """S-shape x {Random, Class-based ABC, Full velocity} must reproduce Module 2 exactly."""
    m2 = pd.read_csv(PROC / "scenario_results.csv").set_index("scenario")
    mine = results[results["routing"] == "S-shape"].set_index("scenario")
    for scenario in (sr.SCENARIO_RANDOM, sr.SCENARIO_CLASS, sr.SCENARIO_VELOCITY):
        a, b = mine.loc[scenario, "avg_distance_per_order_m"], m2.loc[scenario, "avg_distance_per_order_m"]
        if abs(a - b) > 1e-6:
            raise AssertionError(f"S-shape/{scenario} does not match Module 2: {a} != {b}")
    print("Sanity check OK: S-shape x {Random, Class-based ABC, Full velocity} matches "
          "Module 2's scenario_results.csv exactly (933 / 707 / 635 m).")


# ----------------------------------------------------------------------------
# Figure
# ----------------------------------------------------------------------------
def plot_routing_sensitivity(results: pd.DataFrame, colors: dict, scenario_order: list, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(10, 5.4))
    n_scen = len(scenario_order)
    width = 0.8 / n_scen
    x_base = np.arange(len(ROUTING_ORDER))
    y_max = results["avg_distance_per_order_max_m"].max()

    for i, scenario in enumerate(scenario_order):
        xs = x_base + (i - (n_scen - 1) / 2) * width
        sub = results[results["scenario"] == scenario].set_index("routing").reindex(ROUTING_ORDER)
        means = sub["avg_distance_per_order_m"].to_numpy()
        ax.bar(xs, means, width * 0.9, color=colors[scenario], label=scenario, zorder=2)
        multi = sub["n_runs"].to_numpy() > 1
        lo = means - sub["avg_distance_per_order_min_m"].to_numpy()
        hi = sub["avg_distance_per_order_max_m"].to_numpy() - means
        if multi.any():
            ax.errorbar(xs[multi], means[multi], yerr=[lo[multi], hi[multi]], fmt="none",
                        ecolor=INK, elinewidth=1, capsize=3, zorder=3)
        for xi, m in zip(xs, means):
            ax.text(xi, m + y_max * 0.015, f"{m:,.0f}", ha="center", va="bottom", fontsize=7.8,
                    color=INK, rotation=90 if n_scen > 3 else 0)

    ax.set_xticks(x_base)
    ax.set_xticklabels(ROUTING_ORDER, color=INK, fontsize=11)
    ax.set_ylabel("Avg. distance per order (m)", color=INK_MUTED)
    ax.set_ylim(0, y_max * 1.35)
    ax.set_title("Picker travel distance by routing policy and slotting scenario", color=INK, loc="left", fontsize=11.5)
    ax.legend(frameon=False, fontsize=9, loc="upper center", ncol=4, bbox_to_anchor=(0.5, 1.14))
    ax.text(0, -0.1, "Bars = mean over seeds (Random, Class-based ABC) or the single deterministic run "
                     "(Full velocity, Aisle-based velocity); whiskers = min-max over 10 seeds. All orders "
                     "of the last 12 months, same layout as Module 2.",
            transform=ax.transAxes, fontsize=8, color=INK_MUTED)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK_MUTED, length=0)
    ax.yaxis.grid(True, color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> None:
    sr = load_module("02_slotting_routing.py", "slotting_routing")
    PROC.mkdir(parents=True, exist_ok=True)
    FIG.mkdir(parents=True, exist_ok=True)

    layout = sr.build_layout()
    sku, visit_order, visit_sku = sr.load_inputs()
    near_cut = int(round(sr.NEAR_SLOT_SHARE * len(layout)))
    n_orders = int(visit_order.max()) + 1
    print(f"Layout : {len(layout):,} slots | Orders: {n_orders:,} | SKUs: {len(sku):,} | "
          f"order-SKU visits: {len(visit_order):,}")

    runs = run_all(sr, sku, layout, visit_order, visit_sku, near_cut)
    scenario_order = [sr.SCENARIO_RANDOM, sr.SCENARIO_CLASS, sr.SCENARIO_VELOCITY, SCENARIO_AISLE]
    results = summarize(runs, sr.SCENARIO_RANDOM, scenario_order)
    sanity_check_against_module2(sr, results)

    print("\n--- RUNS (one row per routing x scenario x seed) ---")
    print(runs.to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
    print("\n--- ROUTING SENSITIVITY RESULTS ---")
    print(results.to_string(index=False, float_format=lambda v: f"{v:,.4f}"))

    best = (results[results["scenario"] != sr.SCENARIO_RANDOM]
                   .loc[results[results["scenario"] != sr.SCENARIO_RANDOM]
                        .groupby("routing")["avg_distance_per_order_m"].idxmin()])
    print("\n--- BEST (SHORTEST-DISTANCE) SLOTTING SCENARIO PER ROUTING POLICY ---")
    print(best[["routing", "scenario", "avg_distance_per_order_m", "change_vs_random"]]
          .to_string(index=False, float_format=lambda v: f"{v:,.4f}"))

    results.to_csv(PROC / "routing_sensitivity.csv", index=False)
    colors = {**sr.SCENARIO_COLORS, SCENARIO_AISLE: AISLE_COLOR}
    plot_routing_sensitivity(results, colors, scenario_order, FIG / "06_routing_sensitivity.png")
    print(f"\nFigure -> {FIG / '06_routing_sensitivity.png'}\nTable  -> {PROC / 'routing_sensitivity.csv'}")


if __name__ == "__main__":
    main()
