"""
Module 6 - Re-slotting Policy Comparison

Question: given that re-slotting has a cost (every SKU that changes slot has to be physically
moved), which update policy - never, once a year, every quarter, every month, or only when a
SKU's ranking has drifted enough to matter - gives the best distance/labour trade-off, and how
sensitive is that answer to the assumed cost of a single move?

Setup (reuses Module 4's year-over-year split)
  - Starting layout: ranked on the prior year's pick frequency (Dec 2009 - Nov 2010), exactly
    Module 4's "history" snapshot.
  - Evaluated month by month over Dec 2010 - Nov 2011 (12 months, Module 4's "future" year).
  - A policy that "updates" re-ranks SKUs using only data available as of the start of that
    month - a rolling 12-month window ending the day before. No look-ahead: month m's update
    never sees month m's own orders, only month m's orders are then routed with the resulting
    layout. SKUs never picked in the rolling window are ranked last (frequency 0, class C),
    unless a policy explicitly overrides this (see P7).
  - A move = one SKU whose slot changes at an update event. The starting layout is never counted
    (only updates strictly after month 0 count moves).

Policies (P1-P6 are candidates; P7 re-runs two of them with a different new-SKU rule)
  P1  Static ABC             - class-based ABC, ranked once, never updated.
  P2  Static full velocity   - every SKU individually ranked, once, never updated.
  P3  Hybrid static          - nearest N slots individually ranked by velocity, the rest zoned
                                A/B/C as in P1; N in {50, 100, 200, 300, 500}; once, never updated.
  P4  Periodic full velocity - P2's rule, fully re-ranked every month or every quarter.
  P5  Periodic hybrid        - P3's best N; every month or every quarter, only SKUs whose top-N
                                membership or ABC class changed since their last placement move.
  P6  Threshold-based        - P3's best N; monthly check; a SKU moves only if its ABC class
                                changed, or (while staying in the top N) its overall velocity rank
                                has drifted by more than T positions since its last placement;
                                T in {25, 50, 100}.
  P7  New-SKU rule           - P1 and the best of P1-P6, re-run with never-seen SKUs inserted at
                                the front of the B zone instead of placed with the C zone.
  Reference (not policies): Random (10 seeds, never updated) and an Oracle full-velocity layout
  ranked on the evaluation year's OWN frequency (look-ahead; an upper bound only).

Performance: every policy shares ONE precomputed set of 12 rolling monthly pick-frequency/ABC-
class snapshots and 12 monthly order-visit slices (computed once, not per policy). A policy that
only updates once (P1, P2, P3, Random, Oracle) is evaluated over the full year in a single
vectorised call instead of a 12-month loop. This keeps the ~470 policy x seed x period runs well
under a minute of routing computation; no sampling is used.

Input : data/processed/clean_lines.csv, data/processed/sku_velocity.csv, data/raw/online_retail_II.xlsx
Run   : python src/06_reslotting_policies.py   (from repo root, after Module 1)
Output:
  data/processed/reslotting_policies.csv
  reports/figures/07_reslotting_tradeoff.png
"""
import importlib.util
import time
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"
FIG = ROOT / "reports" / "figures"

N_GRID = (50, 100, 200, 300, 500)          # P3 (and P5/P6's inherited "best N")
T_GRID = (25, 50, 100)                     # P6 rank-drift threshold (positions within the top N)
MOVE_TIME_MINUTES = (2, 5, 10)             # labour minutes per SKU moved
WALK_SPEED_M_PER_S = 1.0
QUARTER_MONTHS = (0, 3, 6, 9)              # evaluation-month indices where a quarter starts
ALL_MONTHS = tuple(range(12))
EVAL_MONTH_START0 = pd.Timestamp("2010-12-01")

INK, INK_MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"
ROLE_COLOR = {"policy": "#2a78d6", "reference": "#7a7a7a", "oracle": "#e34948"}


def load_module(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "src" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ----------------------------------------------------------------------------
# Precomputation (shared by every policy)
# ----------------------------------------------------------------------------
def month_start(m: int) -> pd.Timestamp:
    return EVAL_MONTH_START0 + pd.DateOffset(months=m)


def compute_monthly_snapshots(combined: pd.DataFrame, codes: list, m1) -> list:
    """12 rolling 12-month pick-frequency / ABC-class snapshots, one per evaluation month's
    update point. Snapshot m uses only invoices starting in [month_start(m) - 12mo, month_start(m)).
    Snapshot 0's window is exactly Module 4's history year (Dec 2009 - Nov 2010).
    """
    invoice_start = combined.groupby("Invoice")["InvoiceDate"].transform("min")
    combined = combined.assign(_invoice_start=invoice_start)
    snapshots = []
    for m in range(12):
        asof = month_start(m)
        mask = (combined["_invoice_start"] >= asof - pd.DateOffset(months=12)) & (combined["_invoice_start"] < asof)
        pick = combined.loc[mask].groupby("StockCode").size().reindex(codes).fillna(0).astype(int)
        sku_class = m1.abc_class(pick)
        snapshots.append({"pick_lines": pick.to_numpy(), "sku_class": sku_class.to_numpy()})
    return snapshots


def compute_month_visits(future: pd.DataFrame, codes: list, sku_index: pd.Series) -> list:
    """12 (order_idx, sku_idx, n_orders) tuples, one per evaluation month, plus the full-year one."""
    invoice_start = future.groupby("Invoice")["InvoiceDate"].transform("min")
    month_idx = (invoice_start.dt.year - EVAL_MONTH_START0.year) * 12 + (invoice_start.dt.month - EVAL_MONTH_START0.month)
    future = future.assign(_month=month_idx)

    def visits_of(lines):
        v = lines.drop_duplicates(["Invoice", "StockCode"])
        vo = pd.factorize(v["Invoice"])[0]
        vs = sku_index.reindex(v["StockCode"]).to_numpy().astype(int)
        return vo, vs, int(v["Invoice"].nunique())

    months = [visits_of(future[future["_month"] == m]) for m in range(12)]
    full = visits_of(future)
    return months, full


def oracle_snapshot(future: pd.DataFrame, codes: list, m1) -> dict:
    """Full-velocity ranking on the evaluation year's OWN frequency - look-ahead, upper bound only."""
    pick = future.groupby("StockCode").size().reindex(codes).fillna(0).astype(int)
    return {"pick_lines": pick.to_numpy(), "sku_class": m1.abc_class(pick).to_numpy()}


# ----------------------------------------------------------------------------
# Slotting: one hybrid rule covers pure ABC (N=0), pure velocity (N=n_skus) and true hybrids
# ----------------------------------------------------------------------------
def hybrid_ranks(sr, pick_lines: np.ndarray, sku_class: np.ndarray, N: int, rng, new_sku_rule: str = "end") -> np.ndarray:
    """Nearest N slots individually ranked by velocity (ties by SKU index, stable sort); the
    remaining slots zoned A/B/C as in Module 2's slot_class_based (same RNG call pattern, so
    N=0 reproduces slot_class_based exactly and N=n_skus reproduces slot_full_velocity exactly).

    new_sku_rule="start_of_b": SKUs with pick_lines == 0 (never seen in the ranking window) are
    pulled out of C and placed as their own group immediately in front of the regular B group
    (nearer than B, farther than A) - the B block grows by that many slots, C shrinks by the same.
    """
    n = len(pick_lines)
    order = np.argsort(-pick_lines, kind="stable")
    ranks = np.empty(n, dtype=int)
    top_idx = order[:N]
    ranks[top_idx] = np.arange(N)

    rest_mask = np.ones(n, dtype=bool)
    rest_mask[top_idx] = False
    rest_idx = np.flatnonzero(rest_mask)
    labels = sku_class[rest_idx].astype(object).copy()
    if new_sku_rule == "start_of_b":
        is_new = pick_lines[rest_idx] == 0
        labels[is_new] = "B_new"
        fill_order = ["A", "B_new", "B", "C"]
    else:
        fill_order = list(sr.CLASS_ORDER)

    offset = N
    for grp in fill_order:
        idx = rest_idx[labels == grp]
        if len(idx) == 0:
            continue
        ranks[idx] = rng.permutation(np.arange(offset, offset + len(idx))) if rng is not None else np.arange(offset, offset + len(idx))
        offset += len(idx)
    return ranks


def assert_valid_ranks(ranks: np.ndarray, n_slots: int) -> None:
    assert ranks.min() >= 0 and ranks.max() < n_slots and len(set(ranks.tolist())) == len(ranks), "invalid slot assignment"


# ----------------------------------------------------------------------------
# Evaluation helpers
# ----------------------------------------------------------------------------
def dist_sum(sr, ranks, vo, vs, aisle_arr, y_arr) -> float:
    slot = ranks[vs]
    return sr.s_shape_distances(vo, aisle_arr[slot], y_arr[slot]).sum()


# ----------------------------------------------------------------------------
# Policy runners
# ----------------------------------------------------------------------------
def run_random(sr, seeds, n_skus, n_slots, vo_full, vs_full, n_orders_full, aisle_arr, y_arr):
    rows = []
    for seed in seeds:
        ranks = sr.slot_random(n_skus, n_slots, np.random.default_rng(seed))
        assert_valid_ranks(ranks, n_slots)
        d = dist_sum(sr, ranks, vo_full, vs_full, aisle_arr, y_arr)
        rows.append({"policy": "Random (baseline)", "params": "", "role": "reference", "seed": seed,
                    "avg_distance_per_order_m": d / n_orders_full, "total_distance_km": d / 1000, "annual_moves": 0})
    return rows


def run_static(sr, name, params, role, N, seeds, snapshot, vo_full, vs_full, n_orders_full,
               aisle_arr, y_arr, n_slots, new_sku_rule="end"):
    rows = []
    for seed in seeds:
        rng = np.random.default_rng(seed) if seed is not None else None
        ranks = hybrid_ranks(sr, snapshot["pick_lines"], snapshot["sku_class"], N, rng, new_sku_rule)
        assert_valid_ranks(ranks, n_slots)
        d = dist_sum(sr, ranks, vo_full, vs_full, aisle_arr, y_arr)
        rows.append({"policy": name, "params": params, "role": role, "seed": seed,
                    "avg_distance_per_order_m": d / n_orders_full, "total_distance_km": d / 1000, "annual_moves": 0})
    return rows


def run_periodic_full(sr, name, params, N, update_months, monthly_snapshots, month_visits,
                      aisle_arr, y_arr, n_slots, new_sku_rule="end"):
    prev_ranks, ranks, moves, total, n_orders = None, None, 0, 0.0, 0
    for m in range(12):
        if m in update_months:
            new_ranks = hybrid_ranks(sr, monthly_snapshots[m]["pick_lines"], monthly_snapshots[m]["sku_class"],
                                     N, None, new_sku_rule)
            assert_valid_ranks(new_ranks, n_slots)
            if prev_ranks is not None:
                moves += int(np.sum(new_ranks != prev_ranks))
            ranks = new_ranks
            prev_ranks = new_ranks
        vo, vs, norders = month_visits[m]
        total += dist_sum(sr, ranks, vo, vs, aisle_arr, y_arr)
        n_orders += norders
    return [{"policy": name, "params": params, "role": "policy", "seed": None,
             "avg_distance_per_order_m": total / n_orders, "total_distance_km": total / 1000, "annual_moves": moves}]


def run_incremental(sr, name, params, N, seeds, trigger: str, T, update_months, monthly_snapshots,
                    month_visits, aisle_arr, y_arr, n_slots, new_sku_rule="end"):
    """P5 (trigger='class_or_topn') and P6 (trigger='threshold'): only SKUs meeting the trigger
    move; movers reshuffle purely among the slots they collectively vacate, ordered by a fresh
    from-scratch hybrid ranking of the movers only (nearest-desired mover -> nearest vacated slot).
    Non-movers keep their exact previous slot, so physical zone boundaries are free to drift with
    real churn instead of being redrawn (and everyone reshuffled) every period.
    """
    rows = []
    for seed in seeds:
        rng = np.random.default_rng(seed)
        snap0 = monthly_snapshots[0]
        ranks = hybrid_ranks(sr, snap0["pick_lines"], snap0["sku_class"], N, rng, new_sku_rule)
        assert_valid_ranks(ranks, n_slots)
        order0 = np.argsort(-snap0["pick_lines"], kind="stable")
        rank_now0 = np.empty(len(order0), dtype=int)
        rank_now0[order0] = np.arange(len(order0))
        is_topn0 = rank_now0 < N
        labels = np.where(is_topn0, "TOPN", snap0["sku_class"])
        rank_at_placement = rank_now0.copy()

        total, n_orders, moves = 0.0, 0, 0
        for m in range(12):
            if m in update_months and m != 0:
                pick_m, cls_m = monthly_snapshots[m]["pick_lines"], monthly_snapshots[m]["sku_class"]
                order_m = np.argsort(-pick_m, kind="stable")
                rank_now = np.empty(len(order_m), dtype=int)
                rank_now[order_m] = np.arange(len(order_m))
                is_topn_now = rank_now < N
                target_labels = np.where(is_topn_now, "TOPN", cls_m)

                if trigger == "class_or_topn":
                    movers_mask = target_labels != labels
                else:  # "threshold"
                    was_topn = labels == "TOPN"
                    entering, leaving = is_topn_now & ~was_topn, ~is_topn_now & was_topn
                    staying_topn = is_topn_now & was_topn
                    big_shift = staying_topn & (np.abs(rank_now - rank_at_placement) > T)
                    class_changed = (~is_topn_now) & (~was_topn) & (target_labels != labels)
                    movers_mask = entering | leaving | big_shift | class_changed

                if movers_mask.any():
                    ideal = hybrid_ranks(sr, pick_m, cls_m, N, rng, new_sku_rule)
                    movers_idx = np.flatnonzero(movers_mask)
                    movers_sorted = movers_idx[np.argsort(ideal[movers_idx])]
                    vacated = np.sort(ranks[movers_idx])
                    ranks = ranks.copy()
                    ranks[movers_sorted] = vacated
                    assert_valid_ranks(ranks, n_slots)
                    labels = labels.copy()
                    labels[movers_idx] = target_labels[movers_idx]
                    rank_at_placement = rank_at_placement.copy()
                    rank_at_placement[movers_idx] = rank_now[movers_idx]
                    moves += len(movers_idx)
            vo, vs, norders = month_visits[m]
            total += dist_sum(sr, ranks, vo, vs, aisle_arr, y_arr)
            n_orders += norders
        rows.append({"policy": name, "params": params, "role": "policy", "seed": seed,
                    "avg_distance_per_order_m": total / n_orders, "total_distance_km": total / 1000, "annual_moves": moves})
    return rows


# ----------------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------------
def summarize(rows: pd.DataFrame) -> pd.DataFrame:
    g = rows.groupby(["policy", "params", "role"], sort=False)
    out = pd.DataFrame({
        "n_seeds": g["seed"].size(),
        "avg_distance_per_order_m": g["avg_distance_per_order_m"].mean(),
        "avg_distance_per_order_min_m": g["avg_distance_per_order_m"].min(),
        "avg_distance_per_order_max_m": g["avg_distance_per_order_m"].max(),
        "annual_walking_hours": (g["total_distance_km"].mean() * 1000 / WALK_SPEED_M_PER_S / 3600),
        "annual_moves": g["annual_moves"].mean(),
        "annual_moves_min": g["annual_moves"].min(),
        "annual_moves_max": g["annual_moves"].max(),
    }).reset_index()
    for mins in MOVE_TIME_MINUTES:
        out[f"total_time_h_move{mins}min"] = out["annual_walking_hours"] + out["annual_moves"] * mins / 60
    return out


# ----------------------------------------------------------------------------
# Figure: distance vs. moves trade-off, with the Pareto frontier
# ----------------------------------------------------------------------------
def pareto_frontier(points: pd.DataFrame) -> pd.DataFrame:
    """Points (fewer moves, shorter distance = better) that are not dominated by any other point.

    Ties in annual_moves (e.g. every static policy sits at 0) must be broken by distance ascending
    before the running-min sweep, otherwise whichever tied point happens to sort first gets kept
    regardless of whether a cheaper point at the SAME move count exists (e.g. P1/P3/P7 all sit at
    0 moves alongside P2, which has the shortest distance there and should be the only one kept).
    """
    pts = points.sort_values(["annual_moves", "avg_distance_per_order_m"], kind="mergesort")
    best_so_far = np.inf
    keep = []
    for _, row in pts.iterrows():
        if row["avg_distance_per_order_m"] < best_so_far - 1e-9:
            keep.append(True)
            best_so_far = row["avg_distance_per_order_m"]
        else:
            keep.append(False)
    return pts[keep]


# Only these 9 points get a text label (everything else - P3's N grid, P5 quarterly, P6 T=25/50 -
# stays plotted as a dot, unlabelled). Each entry is (dx, dy) in offset points from the point, plus
# horizontal/vertical text alignment, hand-tuned against the rendered PNG so no label overlaps
# another label or a data point (see the module docstring / methodology for the rationale).
LABEL_SPEC = {
    ("Random (baseline)", ""): ("Random (baseline)", 55, 14, "left", "center"),
    ("Oracle (full velocity, look-ahead)", ""): ("Oracle (look-ahead)", 55, -10, "left", "center"),
    ("P1 Static ABC", ""): ("P1 Static ABC", 68, 32, "left", "center"),
    ("P7 P1 + new-SKU-to-B", ""): ("P7 P1 + new-SKU-to-B", 75, 2, "left", "center"),
    ("P2 Static full velocity", ""): ("P2 Static full velocity", 80, -28, "left", "center"),
    ("P5 Periodic hybrid", "N=500, monthly"): ("P5 Hybrid, monthly", -20, 46, "center", "bottom"),
    ("P6 Threshold-based", "N=500, T=100"): ("P6 Threshold, T=100", 55, -42, "left", "center"),
    ("P4 Periodic full velocity", "quarterly"): ("P4 Full velocity, quarterly", 0, 42, "center", "bottom"),
    ("P4 Periodic full velocity", "monthly"): ("P4 Full velocity, monthly", 0, -48, "center", "top"),
}


def plot_tradeoff(results: pd.DataFrame, path: Path) -> None:
    policies = results[results["role"] == "policy"]
    reference = results[results["role"] == "reference"]
    oracle = results[results["role"] == "oracle"]

    fig, ax = plt.subplots(figsize=(10.5, 7))
    # 0-40,161 moves on a linear axis crushes the 1,419-4,131 throttled cluster into <10% of the
    # plot width. symlog spaces that cluster out by relative (multiplicative) difference instead,
    # without needing a second embedded coordinate system (an inset would have to fit its own
    # dots, labels and leader lines inside a small box, risking the same overlap problem again).
    # linthresh keeps 0 (every static policy) on a plain linear segment near the origin.
    ax.set_xscale("symlog", linthresh=300, linscale=0.6)

    ax.scatter(reference["annual_moves"], reference["avg_distance_per_order_m"], color=ROLE_COLOR["reference"],
              marker="s", s=70, zorder=3, label="Random (baseline)")
    ax.scatter(oracle["annual_moves"], oracle["avg_distance_per_order_m"], color=ROLE_COLOR["oracle"],
              marker="*", s=110, zorder=3, label="Oracle (look-ahead, not a policy)")
    ax.scatter(policies["annual_moves"], policies["avg_distance_per_order_m"], color=ROLE_COLOR["policy"],
              s=55, zorder=3, label="Candidate policy (P1-P7)")

    frontier = pareto_frontier(pd.concat([policies, reference]))
    ax.plot(frontier["annual_moves"], frontier["avg_distance_per_order_m"], color=INK, lw=1.3,
           ls="--", zorder=2, label="Efficient frontier")

    for _, row in results.iterrows():
        spec = LABEL_SPEC.get((row["policy"], row["params"]))
        if spec is None:
            continue
        text, dx, dy, ha, va = spec
        ax.annotate(text, xy=(row["annual_moves"], row["avg_distance_per_order_m"]),
                   xytext=(dx, dy), textcoords="offset points", fontsize=8.3, color=INK, ha=ha, va=va,
                   arrowprops=dict(arrowstyle="-", color=INK_MUTED, lw=0.8, shrinkA=0, shrinkB=4,
                                   connectionstyle="arc3,rad=0.08"))

    ax.set_xlabel("Annual moves (SKUs re-slotted)", color=INK_MUTED)
    ax.set_ylabel("Avg. distance per order (m)", color=INK_MUTED)
    ax.set_title("Re-slotting policies: distance vs. move-count trade-off", color=INK, loc="left", fontsize=11.5)
    ax.legend(frameon=False, fontsize=9, loc="upper right")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK_MUTED, length=0)
    ax.grid(True, color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> None:
    t0 = time.time()
    sr = load_module("02_slotting_routing.py", "slotting_routing")
    m1 = load_module("01_order_profile_abc.py", "order_profile_abc")
    m4 = load_module("04_out_of_sample_check.py", "out_of_sample_check")
    PROC.mkdir(parents=True, exist_ok=True)
    FIG.mkdir(parents=True, exist_ok=True)

    history_raw, future_raw, dup_info = m4.combined_deduped_lines(m1)
    combined = pd.concat([history_raw, future_raw], ignore_index=True)
    history, future, _ = m4.year_over_year_periods(m1)   # future = the windowed evaluation year
    codes = sorted(future["StockCode"].unique())
    n_skus = len(codes)
    sku_index = pd.Series(np.arange(n_skus), index=codes)

    layout, n_aisles = m4.size_layout(sr, n_skus)
    n_slots = len(layout)
    aisle_arr, y_arr = layout["aisle"].to_numpy(), layout["y_m"].to_numpy()
    print(f"SKU universe: {n_skus:,} (evaluation year) | Layout: {n_aisles} aisles, {n_slots:,} slots")

    monthly_snapshots = compute_monthly_snapshots(combined, codes, m1)
    month_visits, (vo_full, vs_full, n_orders_full) = compute_month_visits(future, codes, sku_index)
    oracle_snap = oracle_snapshot(future, codes, m1)
    print(f"Evaluation months: Dec 2010 - Nov 2011 | Orders: {n_orders_full:,} | "
          f"Month-0 (starting) pick_lines total: {monthly_snapshots[0]['pick_lines'].sum():,} "
          f"(should equal the history-year total)")
    print(f"Precompute done in {time.time() - t0:.1f}s")

    seeds10 = list(sr.RANDOM_SEEDS)
    rows = []
    rows += run_random(sr, seeds10, n_skus, n_slots, vo_full, vs_full, n_orders_full, aisle_arr, y_arr)
    rows += run_static(sr, "Oracle (full velocity, look-ahead)", "", "oracle", n_skus, [None], oracle_snap,
                       vo_full, vs_full, n_orders_full, aisle_arr, y_arr, n_slots)

    # P1: static ABC
    rows += run_static(sr, "P1 Static ABC", "", "policy", 0, seeds10, monthly_snapshots[0],
                       vo_full, vs_full, n_orders_full, aisle_arr, y_arr, n_slots)
    # P2: static full velocity
    rows += run_static(sr, "P2 Static full velocity", "", "policy", n_skus, [None], monthly_snapshots[0],
                       vo_full, vs_full, n_orders_full, aisle_arr, y_arr, n_slots)
    # P3: hybrid static, N grid
    for N in N_GRID:
        rows += run_static(sr, "P3 Hybrid static", f"N={N}", "policy", N, seeds10, monthly_snapshots[0],
                           vo_full, vs_full, n_orders_full, aisle_arr, y_arr, n_slots)

    p1_p6 = summarize(pd.DataFrame(rows))
    p3_rows = p1_p6[p1_p6["policy"] == "P3 Hybrid static"]
    best_N = int(p3_rows.loc[p3_rows["avg_distance_per_order_m"].idxmin(), "params"].split("=")[1])
    print(f"\nP3 best N (lowest avg. distance per order): N={best_N}")

    # P4: periodic full velocity, monthly / quarterly
    rows += run_periodic_full(sr, "P4 Periodic full velocity", "monthly", n_skus, ALL_MONTHS,
                              monthly_snapshots, month_visits, aisle_arr, y_arr, n_slots)
    rows += run_periodic_full(sr, "P4 Periodic full velocity", "quarterly", n_skus, QUARTER_MONTHS,
                              monthly_snapshots, month_visits, aisle_arr, y_arr, n_slots)

    # P5: periodic hybrid (best N), only top-N/class changes move; monthly / quarterly
    rows += run_incremental(sr, "P5 Periodic hybrid", f"N={best_N}, monthly", best_N, seeds10, "class_or_topn",
                            None, ALL_MONTHS, monthly_snapshots, month_visits, aisle_arr, y_arr, n_slots)
    rows += run_incremental(sr, "P5 Periodic hybrid", f"N={best_N}, quarterly", best_N, seeds10, "class_or_topn",
                            None, QUARTER_MONTHS, monthly_snapshots, month_visits, aisle_arr, y_arr, n_slots)

    # P6: threshold-based (best N), monthly, T grid
    for T in T_GRID:
        rows += run_incremental(sr, "P6 Threshold-based", f"N={best_N}, T={T}", best_N, seeds10, "threshold",
                                T, ALL_MONTHS, monthly_snapshots, month_visits, aisle_arr, y_arr, n_slots)

    results_so_far = summarize(pd.DataFrame(rows))
    candidates = results_so_far[results_so_far["role"] == "policy"]
    best_row = candidates.loc[candidates["avg_distance_per_order_m"].idxmin()]
    best_policy, best_params = best_row["policy"], best_row["params"]
    print(f"Best policy P1-P6 by avg. distance per order: {best_policy} ({best_params}) "
          f"= {best_row['avg_distance_per_order_m']:.1f} m")

    # P7: P1 and the best P1-P6 policy, re-run with new SKUs inserted at the front of the B zone
    rows += run_static(sr, "P7 P1 + new-SKU-to-B", "", "policy", 0, seeds10, monthly_snapshots[0],
                       vo_full, vs_full, n_orders_full, aisle_arr, y_arr, n_slots, new_sku_rule="start_of_b")
    if best_policy == "P1 Static ABC":
        print("P7's 'best policy' variant is skipped: the best P1-P6 policy is already P1.")
    elif best_policy == "P2 Static full velocity":
        rows += run_static(sr, f"P7 {best_policy} + new-SKU-to-B", "", "policy", n_skus, [None], monthly_snapshots[0],
                           vo_full, vs_full, n_orders_full, aisle_arr, y_arr, n_slots, new_sku_rule="start_of_b")
    elif best_policy == "P3 Hybrid static":
        N = int(best_params.split("=")[1])
        rows += run_static(sr, f"P7 {best_policy} + new-SKU-to-B", best_params, "policy", N, seeds10,
                           monthly_snapshots[0], vo_full, vs_full, n_orders_full, aisle_arr, y_arr, n_slots,
                           new_sku_rule="start_of_b")
    elif best_policy == "P4 Periodic full velocity":
        freq_months = ALL_MONTHS if "monthly" in best_params else QUARTER_MONTHS
        rows += run_periodic_full(sr, f"P7 {best_policy} + new-SKU-to-B", best_params, n_skus, freq_months,
                                  monthly_snapshots, month_visits, aisle_arr, y_arr, n_slots, new_sku_rule="start_of_b")
    elif best_policy == "P5 Periodic hybrid":
        freq_months = ALL_MONTHS if "monthly" in best_params else QUARTER_MONTHS
        rows += run_incremental(sr, f"P7 {best_policy} + new-SKU-to-B", best_params, best_N, seeds10, "class_or_topn",
                                None, freq_months, monthly_snapshots, month_visits, aisle_arr, y_arr, n_slots,
                                new_sku_rule="start_of_b")
    elif best_policy == "P6 Threshold-based":
        T = int(best_params.split("T=")[1])
        rows += run_incremental(sr, f"P7 {best_policy} + new-SKU-to-B", best_params, best_N, seeds10, "threshold",
                                T, ALL_MONTHS, monthly_snapshots, month_visits, aisle_arr, y_arr, n_slots,
                                new_sku_rule="start_of_b")

    runs = pd.DataFrame(rows)
    results = summarize(runs)
    results = results.sort_values(["role", "policy", "params"], kind="mergesort").reset_index(drop=True)

    # --- Sanity checks against Module 4's year-over-year results ---
    oos = pd.read_csv(PROC / "out_of_sample_results.csv")
    oos_yoy = oos[(oos["split"] == "year-over-year") & (oos["visit_scope"] == "all_visits")]

    def check(name, params, oos_scenario, oos_basis):
        mine = results[(results["policy"] == name) & (results["params"] == params)]["avg_distance_per_order_m"].iloc[0]
        theirs = oos_yoy[(oos_yoy["scenario"] == oos_scenario) & (oos_yoy["ranking_basis"] == oos_basis)]["avg_distance_per_order_m"].iloc[0]
        assert abs(mine - theirs) < 1e-6, f"{name} {params}: {mine} != Module 4's {theirs}"
        return mine

    check("P1 Static ABC", "", "Class-based ABC", "out_of_sample")
    check("P2 Static full velocity", "", "Full velocity", "out_of_sample")
    check("Oracle (full velocity, look-ahead)", "", "Full velocity", "in_sample")
    print("\nSanity check OK: P1, P2 and Oracle reproduce Module 4's year-over-year "
          "Class-based-ABC/out-of-sample, Full-velocity/out-of-sample and Full-velocity/in-sample exactly.")

    print(f"\nTotal runtime: {time.time() - t0:.1f}s (precompute + {len(runs)} policy x seed runs, no sampling)")

    print("\n--- RESLOTTING POLICY RESULTS ---")
    print(results.drop(columns=["annual_moves_min", "annual_moves_max"])
          .to_string(index=False, float_format=lambda v: f"{v:,.2f}"))

    print("\n--- BEST POLICY PER MOVE-TIME ASSUMPTION (excluding Random and Oracle) ---")
    candidates = results[results["role"] == "policy"]
    for mins in MOVE_TIME_MINUTES:
        col = f"total_time_h_move{mins}min"
        row = candidates.loc[candidates[col].idxmin()]
        print(f"  {mins:>2} min/move -> {row['policy']} ({row['params']}): "
              f"{row[col]:,.0f} h/year total (walking {row['annual_walking_hours']:,.0f} h + "
              f"{row['annual_moves']:,.0f} moves)")

    results.to_csv(PROC / "reslotting_policies.csv", index=False)
    plot_tradeoff(results, FIG / "07_reslotting_tradeoff.png")
    print(f"\nFigure -> {FIG / '07_reslotting_tradeoff.png'}\nTable  -> {PROC / 'reslotting_policies.csv'}")


if __name__ == "__main__":
    main()
