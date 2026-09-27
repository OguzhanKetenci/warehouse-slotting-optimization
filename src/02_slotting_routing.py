"""
Module 2 - Synthetic Warehouse Layout, Slotting Scenarios and S-shape Routing

Input : data/processed/clean_lines.csv, data/processed/sku_velocity.csv  (Module 1)
Run   : python src/02_slotting_routing.py   (from repo root)
Output:
  data/processed/scenario_runs.csv              (one row per scenario x seed)
  data/processed/scenario_results.csv           (one row per scenario, input for Module 3)
  data/processed/slot_assignment_velocity.csv   (SKU -> slot, full-velocity scenario)
  reports/figures/03_distance_by_scenario.png
  reports/figures/04_heatmap_random_vs_velocity.png

Warehouse model (all distances in metres)
  - Single block, parallel aisles, one front and one back cross aisle.
  - Depot at the left end of the front cross aisle, x = 0, y = 0.
  - Aisle j has its centre line at x = FIRST_AISLE_X_M + j * AISLE_PITCH_M.
  - Slot k (0-based) on either side of an aisle has its centre at y = (k + 0.5) * SLOT_WIDTH_M.
  - Walking distance depot -> slot = x + y (rectilinear, via front cross aisle and aisle).
  - Cross-aisle width and the lateral zig-zag between the two racks of an aisle are ignored.
"""
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap, LogNorm

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"
FIG = ROOT / "reports" / "figures"

# ----------------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------------
N_AISLES = 40                 # parallel aisles
SIDES_PER_AISLE = 2           # racks on the left and right of each aisle
SLOTS_PER_SIDE = 50           # slots per rack -> 40 x 2 x 50 = 4,000 slots (>= 3,791 SKUs)
SLOT_WIDTH_M = 1.0
AISLE_PITCH_M = 3.0           # distance between aisle centre lines
FIRST_AISLE_X_M = 1.5         # x of the first aisle centre line; depot is at x = 0
AISLE_LENGTH_M = SLOTS_PER_SIDE * SLOT_WIDTH_M

RANDOM_SEEDS = tuple(range(10))   # random baseline and class-based within-zone shuffles
NEAR_SLOT_SHARE = 0.20            # "near" slots = nearest 20% of all slots by walking distance
CLASS_ORDER = ("A", "B", "C")

SCENARIO_RANDOM = "Random (baseline)"
SCENARIO_CLASS = "Class-based ABC"
SCENARIO_VELOCITY = "Full velocity"

HEATMAP_RANDOM_SEED = RANDOM_SEEDS[0]

# Colours: categorical slots 1-3 of the reference palette, sequential blue ramp.
SCENARIO_COLORS = {SCENARIO_RANDOM: "#2a78d6", SCENARIO_CLASS: "#eb6834", SCENARIO_VELOCITY: "#1baf7a"}
SEQUENTIAL_BLUE = ["#cde2fb", "#86b6ef", "#3987e5", "#256abf", "#184f95", "#0d366b"]
INK, INK_MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"


# ----------------------------------------------------------------------------
# Layout
# ----------------------------------------------------------------------------
def build_layout(n_aisles: int = N_AISLES) -> pd.DataFrame:
    """All slots, sorted by walking distance from the depot (row index = slot rank, 0 = nearest).

    Ties are broken by aisle, then position, then side, so the ordering is deterministic.
    `n_aisles` defaults to the module constant; Module 4 passes a larger value if a SKU universe
    restricted to fewer SKUs than the full catalogue still needs more slots than the default layout.
    """
    aisle, side, pos = np.meshgrid(np.arange(n_aisles), np.arange(SIDES_PER_AISLE),
                                   np.arange(SLOTS_PER_SIDE), indexing="ij")
    layout = pd.DataFrame({"aisle": aisle.ravel(), "side": side.ravel(), "pos": pos.ravel()})
    layout["x_m"] = FIRST_AISLE_X_M + AISLE_PITCH_M * layout["aisle"]
    layout["y_m"] = (layout["pos"] + 0.5) * SLOT_WIDTH_M
    layout["walk_m"] = layout["x_m"] + layout["y_m"]
    return (layout.sort_values(["walk_m", "aisle", "pos", "side"], kind="mergesort")
                  .reset_index(drop=True))


# ----------------------------------------------------------------------------
# Inputs
# ----------------------------------------------------------------------------
def load_inputs():
    """SKU table (fast -> slow) and distinct (order, SKU) location visits as integer index arrays."""
    sku = pd.read_csv(PROC / "sku_velocity.csv", dtype={"StockCode": str})
    sku = (sku.sort_values(["pick_lines", "StockCode"], ascending=[False, True], kind="mergesort")
              .reset_index(drop=True))

    lines = pd.read_csv(PROC / "clean_lines.csv", usecols=["Invoice", "StockCode"], dtype=str)
    visits = lines.drop_duplicates()   # two lines of one SKU in one order = one visit to the slot

    sku_index = pd.Series(np.arange(len(sku)), index=sku["StockCode"])
    visit_sku = sku_index.reindex(visits["StockCode"]).to_numpy()
    if np.isnan(visit_sku.astype(float)).any():
        raise ValueError("clean_lines.csv contains SKUs missing from sku_velocity.csv")
    visit_order = pd.factorize(visits["Invoice"])[0]
    return sku, visit_order, visit_sku.astype(int)


# ----------------------------------------------------------------------------
# Slotting policies: each returns, per SKU, the slot rank (index into the layout)
# ----------------------------------------------------------------------------
def slot_random(n_skus: int, n_slots: int, rng: np.random.Generator) -> np.ndarray:
    """SKUs occupy a random subset of slots."""
    return rng.permutation(n_slots)[:n_skus]


def slot_class_based(sku_class: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """A SKUs fill the nearest slots, then B, then C; placement inside each zone is random."""
    ranks = np.empty(len(sku_class), dtype=int)
    start = 0
    for cls in CLASS_ORDER:
        idx = np.flatnonzero(sku_class == cls)
        ranks[idx] = rng.permutation(np.arange(start, start + len(idx)))
        start += len(idx)
    return ranks


def slot_full_velocity(pick_lines: np.ndarray) -> np.ndarray:
    """Fastest SKU takes the nearest slot, second fastest the second nearest, and so on."""
    order = np.argsort(-pick_lines, kind="stable")
    ranks = np.empty(len(pick_lines), dtype=int)
    ranks[order] = np.arange(len(pick_lines))
    return ranks


# ----------------------------------------------------------------------------
# S-shape routing
# ----------------------------------------------------------------------------
def s_shape_distance(aisles, ys) -> float:
    """S-shape route length for ONE order (reference implementation).

    aisles : 0-based aisle index of every pick;  ys : pick position along the aisle in metres.
    Every aisle containing a pick is traversed end to end. If the number of such aisles is
    odd, the last (rightmost) aisle is entered from the front and left again from its
    farthest pick. The route starts and ends at the depot.

    The horizontal part always equals 2 * x(rightmost aisle); the vertical part is one full
    aisle length per traversed aisle, plus 2 * farthest pick for a half-entered last aisle.
    """
    farthest = {}
    for a, y in zip(aisles, ys):
        farthest[a] = max(farthest.get(a, 0.0), y)
    n = len(farthest)
    last = max(farthest)
    odd = n % 2
    horizontal = 2 * (FIRST_AISLE_X_M + AISLE_PITCH_M * last)
    vertical = AISLE_LENGTH_M * (n - odd) + 2 * farthest[last] * odd
    return horizontal + vertical


def s_shape_distances(order_idx: np.ndarray, aisle: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Vectorised S-shape length for every order (same formula as s_shape_distance)."""
    picks = pd.DataFrame({"order": order_idx, "aisle": aisle, "y": y})
    per_aisle = picks.groupby(["order", "aisle"], sort=True, as_index=False)["y"].max()
    n_aisles = per_aisle.groupby("order", sort=True)["aisle"].size().to_numpy()
    last = per_aisle.drop_duplicates("order", keep="last")      # rightmost aisle of each order
    odd = n_aisles % 2
    horizontal = 2 * (FIRST_AISLE_X_M + AISLE_PITCH_M * last["aisle"].to_numpy())
    vertical = AISLE_LENGTH_M * (n_aisles - odd) + 2 * last["y"].to_numpy() * odd
    return horizontal + vertical


# ----------------------------------------------------------------------------
# Scenario evaluation
# ----------------------------------------------------------------------------
def evaluate(sku_rank, layout, visit_order, visit_sku, near_cut):
    """Average distance per order (m), total distance (km) and share of picks from near slots."""
    slot = sku_rank[visit_sku]
    dist = s_shape_distances(visit_order, layout["aisle"].to_numpy()[slot], layout["y_m"].to_numpy()[slot])
    return dist.mean(), dist.sum() / 1000.0, float((slot < near_cut).mean())


def run_scenarios(sku, layout, visit_order, visit_sku):
    n_skus, n_slots = len(sku), len(layout)
    if n_slots < n_skus:
        raise ValueError(f"layout has {n_slots} slots for {n_skus} SKUs")
    near_cut = int(round(NEAR_SLOT_SHARE * n_slots))
    sku_class = sku["abc_pick"].to_numpy()
    assignments = {}   # (scenario, seed) -> slot ranks, kept for figures

    rows = []
    def record(scenario, seed, ranks):
        avg, total_km, near = evaluate(ranks, layout, visit_order, visit_sku, near_cut)
        rows.append({"scenario": scenario, "seed": seed, "avg_distance_per_order_m": avg,
                     "total_distance_km": total_km, "share_picks_nearest_20pct_slots": near})
        assignments[(scenario, seed)] = ranks

    for seed in RANDOM_SEEDS:
        record(SCENARIO_RANDOM, seed, slot_random(n_skus, n_slots, np.random.default_rng(seed)))
    for seed in RANDOM_SEEDS:
        record(SCENARIO_CLASS, seed, slot_class_based(sku_class, np.random.default_rng(seed)))
    record(SCENARIO_VELOCITY, None, slot_full_velocity(sku["pick_lines"].to_numpy()))
    runs = pd.DataFrame(rows)
    runs["seed"] = runs["seed"].astype("Int64")   # blank for the deterministic scenario
    return runs, assignments, near_cut


def summarize(runs: pd.DataFrame) -> pd.DataFrame:
    g = runs.groupby("scenario", sort=False)
    out = pd.DataFrame({
        "n_runs": g["total_distance_km"].size(),
        "avg_distance_per_order_m": g["avg_distance_per_order_m"].mean(),
        "avg_distance_per_order_min_m": g["avg_distance_per_order_m"].min(),
        "avg_distance_per_order_max_m": g["avg_distance_per_order_m"].max(),
        "total_distance_km": g["total_distance_km"].mean(),
        "total_distance_min_km": g["total_distance_km"].min(),
        "total_distance_max_km": g["total_distance_km"].max(),
        "share_picks_nearest_20pct_slots": g["share_picks_nearest_20pct_slots"].mean(),
    })
    out["change_vs_random"] = out["total_distance_km"] / out.loc[SCENARIO_RANDOM, "total_distance_km"] - 1
    return out.reset_index()


def velocity_assignment_table(sku, layout, ranks) -> pd.DataFrame:
    """Human-readable slot assignment (1-based aisle and position; side L/R)."""
    slot = layout.iloc[ranks].reset_index(drop=True)
    return pd.DataFrame({
        "velocity_rank": np.arange(1, len(sku) + 1),
        "StockCode": sku["StockCode"], "description": sku["description"],
        "pick_lines": sku["pick_lines"], "abc_pick": sku["abc_pick"],
        "aisle": slot["aisle"] + 1, "side": np.where(slot["side"] == 0, "L", "R"),
        "position": slot["pos"] + 1, "walk_distance_m": slot["walk_m"],
    })


# ----------------------------------------------------------------------------
# Figures
# ----------------------------------------------------------------------------
def _style(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK_MUTED, length=0)
    ax.yaxis.grid(True, color=GRID, lw=0.8)
    ax.set_axisbelow(True)


def plot_distance_by_scenario(summary: pd.DataFrame, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(7.5, 4.6))
    x = np.arange(len(summary))
    means = summary["avg_distance_per_order_m"].to_numpy()
    lo = means - summary["avg_distance_per_order_min_m"].to_numpy()
    hi = summary["avg_distance_per_order_max_m"].to_numpy() - means
    colors = [SCENARIO_COLORS[s] for s in summary["scenario"]]
    ax.bar(x, means, width=0.55, color=colors, zorder=2)
    multi = summary["n_runs"].to_numpy() > 1
    ax.errorbar(x[multi], means[multi], yerr=[lo[multi], hi[multi]], fmt="none",
                ecolor=INK, elinewidth=1.2, capsize=5, zorder=3)
    for xi, row, top in zip(x, summary.itertuples(), means + hi):
        change = "baseline" if row.scenario == SCENARIO_RANDOM else f"{row.change_vs_random:+.1%} vs. random"
        ax.text(xi, top + means.max() * 0.02, f"{row.avg_distance_per_order_m:,.0f} m\n{change}",
                ha="center", va="bottom", fontsize=9, color=INK)
    ax.set_xticks(x)
    ax.set_xticklabels([f"{s}\n({int(n)} seeds)" if n > 1 else f"{s}\n(deterministic)"
                        for s, n in zip(summary["scenario"], summary["n_runs"])], color=INK, fontsize=9)
    ax.set_ylabel("Avg. S-shape distance per order (m)", color=INK_MUTED)
    ax.set_ylim(0, means.max() * 1.25)
    ax.set_title("Picker travel distance by slotting scenario", color=INK, loc="left", fontsize=11)
    ax.text(0, -0.2, f"Bars = mean over seeds; whiskers = min-max across the {len(RANDOM_SEEDS)} seeds. "
                     "All orders of the last 12 months.", transform=ax.transAxes, fontsize=8, color=INK_MUTED)
    _style(ax)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def pick_density_grid(ranks, visits_per_sku, layout) -> np.ndarray:
    """Pick visits per slot on a 1 m grid; rack columns are 3j and 3j+2, the walkway is 3j+1."""
    grid = np.full((SLOTS_PER_SIDE, int(N_AISLES * AISLE_PITCH_M)), np.nan)
    slot = layout.iloc[ranks]
    col = (slot["aisle"] * int(AISLE_PITCH_M) + np.where(slot["side"] == 0, 0, 2)).to_numpy()
    grid[slot["pos"].to_numpy(), col] = visits_per_sku
    return grid


def plot_heatmaps(grids, titles, path: Path) -> None:
    cmap = LinearSegmentedColormap.from_list("blue_seq", SEQUENTIAL_BLUE)
    cmap.set_bad("#f4f3f0")
    vmax = np.nanmax([np.nanmax(g) for g in grids])
    norm = LogNorm(vmin=1, vmax=vmax)
    width, length = N_AISLES * AISLE_PITCH_M, AISLE_LENGTH_M

    fig, axes = plt.subplots(1, 2, figsize=(15, 4.4), sharey=True)
    for ax, grid, title in zip(axes, grids, titles):
        im = ax.imshow(grid, origin="lower", extent=[0, width, 0, length], cmap=cmap, norm=norm,
                       aspect="equal", interpolation="nearest")
        ax.plot([0], [-2.5], marker="*", ms=11, color=INK, clip_on=False)
        ax.annotate("Depot", (0, -2.5), xytext=(4, -7.5), fontsize=8, color=INK, annotation_clip=False)
        ax.set_title(title, color=INK, loc="left", fontsize=11)
        ax.set_xlabel("Distance along cross aisle (m)", color=INK_MUTED)
        ax.tick_params(colors=INK_MUTED)
        for s in ax.spines.values():
            s.set_visible(False)
    axes[0].set_ylabel("Depth into aisle (m)", color=INK_MUTED)
    cbar = fig.colorbar(im, ax=axes, location="bottom", shrink=0.45, aspect=40, pad=0.2)
    cbar.set_label("Pick visits per slot, last 12 months (log scale)", color=INK_MUTED)
    cbar.ax.tick_params(colors=INK_MUTED)
    fig.text(0.5, -0.02, "Light grey = walkway or empty slot. Each aisle has a left and a right rack (1 m slots).",
             ha="center", fontsize=8, color=INK_MUTED)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> None:
    PROC.mkdir(parents=True, exist_ok=True)
    FIG.mkdir(parents=True, exist_ok=True)

    layout = build_layout()
    sku, visit_order, visit_sku = load_inputs()
    n_orders = int(visit_order.max()) + 1
    print(f"Layout : {N_AISLES} aisles x {SIDES_PER_AISLE} sides x {SLOTS_PER_SIDE} slots = {len(layout):,} slots "
          f"({len(layout) - len(sku):,} spare), aisle length {AISLE_LENGTH_M:.0f} m")
    print(f"Orders : {n_orders:,} | SKUs: {len(sku):,} | distinct order-SKU visits: {len(visit_order):,}")
    print(f"Slot ordering: nearest slot {layout['walk_m'].iloc[0]:.1f} m, farthest {layout['walk_m'].iloc[-1]:.1f} m from depot")

    runs, assignments, _ = run_scenarios(sku, layout, visit_order, visit_sku)
    summary = summarize(runs)

    print("\n--- RUNS (one row per scenario x seed) ---")
    print(runs.to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
    print("\n--- SCENARIO RESULTS ---")
    print(summary.to_string(index=False, float_format=lambda v: f"{v:,.4f}"))

    runs.to_csv(PROC / "scenario_runs.csv", index=False)
    summary.to_csv(PROC / "scenario_results.csv", index=False)
    velocity_ranks = assignments[(SCENARIO_VELOCITY, None)]
    velocity_assignment_table(sku, layout, velocity_ranks).to_csv(PROC / "slot_assignment_velocity.csv", index=False)

    visits_per_sku = np.bincount(visit_sku, minlength=len(sku))
    grids = [pick_density_grid(assignments[(SCENARIO_RANDOM, HEATMAP_RANDOM_SEED)], visits_per_sku, layout),
             pick_density_grid(velocity_ranks, visits_per_sku, layout)]
    plot_distance_by_scenario(summary, FIG / "03_distance_by_scenario.png")
    plot_heatmaps(grids, [f"Random assignment (seed {HEATMAP_RANDOM_SEED})", "Full velocity assignment"],
                  FIG / "04_heatmap_random_vs_velocity.png")
    print(f"\nFigures -> {FIG}\nTables  -> {PROC}")


if __name__ == "__main__":
    main()
