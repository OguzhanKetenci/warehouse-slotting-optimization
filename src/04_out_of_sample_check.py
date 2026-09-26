"""
Module 4 - Out-of-sample check of the slotting scenarios

Question: Modules 2-3 rank SKUs with the same 12 months they are evaluated on (in-sample).
How much of the saving survives when SKUs are ranked on past orders only?

Design
  - Orders before SPLIT_DATE = "history" (about the first 6 months); orders on/after = "evaluation".
  - All scenarios are evaluated on the evaluation orders only, with the Module 2 layout and S-shape routing.
  - In-sample     : pick frequency and ABC classes computed from the evaluation orders themselves.
  - Out-of-sample : pick frequency and ABC classes computed from the history orders only.
  - Random baseline: 10 seeds (no ranking information). SKUs without picks in the ranking period get
    frequency 0, class C, and are placed last (ties by SKU code).

Input : data/processed/clean_lines.csv, data/processed/sku_velocity.csv  (Module 1)
Run   : python src/04_out_of_sample_check.py   (from repo root, after Module 1)
Output:
  data/processed/out_of_sample_results.csv
  reports/figures/05_in_vs_out_of_sample.png
"""
import importlib.util
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import to_rgb
from matplotlib.patches import Patch

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"
FIG = ROOT / "reports" / "figures"

SPLIT_DATE = pd.Timestamp("2011-06-01")   # history: Dec 2010 - May 2011, evaluation: Jun - Dec 2011

BASIS_NONE, BASIS_IN, BASIS_OUT = "none", "in_sample", "out_of_sample"
BASIS_LABEL = {BASIS_IN: "In-sample (ranked on the evaluation period)",
               BASIS_OUT: "Out-of-sample (ranked on the previous 6 months)"}
INK, INK_MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"


def load_module(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "src" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def split_periods():
    """Split cleaned order lines into history and evaluation by each invoice's timestamp."""
    lines = pd.read_csv(PROC / "clean_lines.csv", usecols=["Invoice", "StockCode", "InvoiceDate"],
                        dtype={"Invoice": str, "StockCode": str}, parse_dates=["InvoiceDate"])
    invoice_start = lines.groupby("Invoice")["InvoiceDate"].transform("min")
    history_mask = invoice_start < SPLIT_DATE
    history, evaluation = lines[history_mask], lines[~history_mask]
    assert not set(history["Invoice"]) & set(evaluation["Invoice"]), "an invoice spans both periods"
    return history, evaluation


def sku_stats(period_lines: pd.DataFrame, codes: list, abc_class):
    """Pick lines and ABC class (Module 1 definition) per SKU from one period."""
    pick_lines = period_lines.groupby("StockCode").size().reindex(codes).fillna(0).astype(int)
    return pick_lines.to_numpy(), abc_class(pick_lines).to_numpy()


def order_visits(period_lines: pd.DataFrame, sku_index: pd.Series):
    """Distinct (order, SKU) visits as integer index arrays."""
    visits = period_lines.drop_duplicates(["Invoice", "StockCode"])
    return pd.factorize(visits["Invoice"])[0], sku_index.reindex(visits["StockCode"]).to_numpy().astype(int)


def run_all(sr, abc_class, history, evaluation, codes):
    layout = sr.build_layout()
    near_cut = int(round(sr.NEAR_SLOT_SHARE * len(layout)))
    sku_index = pd.Series(np.arange(len(codes)), index=codes)
    visit_order, visit_sku = order_visits(evaluation, sku_index)
    history_pick, history_cls = sku_stats(history, codes, abc_class)
    eval_pick, eval_cls = sku_stats(evaluation, codes, abc_class)

    rows = []
    def record(scenario, basis, seed, ranks):
        avg, total_km, near = sr.evaluate(ranks, layout, visit_order, visit_sku, near_cut)
        rows.append({"scenario": scenario, "ranking_basis": basis, "seed": seed,
                     "avg_distance_per_order_m": avg, "total_distance_km": total_km,
                     "share_picks_nearest_20pct_slots": near})

    for seed in sr.RANDOM_SEEDS:
        record(sr.SCENARIO_RANDOM, BASIS_NONE, seed, sr.slot_random(len(codes), len(layout), np.random.default_rng(seed)))
    for basis, pick, cls in ((BASIS_IN, eval_pick, eval_cls), (BASIS_OUT, history_pick, history_cls)):
        for seed in sr.RANDOM_SEEDS:
            record(sr.SCENARIO_CLASS, basis, seed, sr.slot_class_based(cls, np.random.default_rng(seed)))
        record(sr.SCENARIO_VELOCITY, basis, None, sr.slot_full_velocity(pick))

    unseen = history_pick[visit_sku] == 0   # evaluation visits to SKUs never picked in the history period
    info = {"history_orders": history["Invoice"].nunique(), "evaluation_orders": evaluation["Invoice"].nunique(),
            "evaluation_visits": len(visit_order),
            "unseen_skus": int(((history_pick == 0) & (eval_pick > 0)).sum()),
            "unseen_visit_share": float(unseen.mean()),
            "abc_counts_history": {c: int((history_cls == c).sum()) for c in sr.CLASS_ORDER},
            "abc_counts_evaluation": {c: int((eval_cls == c).sum()) for c in sr.CLASS_ORDER}}
    runs = pd.DataFrame(rows)
    runs["seed"] = runs["seed"].astype("Int64")
    return runs, info


def summarize(runs: pd.DataFrame, random_name: str, order: list) -> pd.DataFrame:
    base = runs.loc[runs["scenario"] == random_name, "total_distance_km"].mean()
    runs = runs.assign(change=runs["total_distance_km"] / base - 1)
    g = runs.groupby(["scenario", "ranking_basis"], sort=False)
    out = pd.DataFrame({
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
    key = out.apply(lambda r: order.index((r["scenario"], r["ranking_basis"])), axis=1)
    return out.assign(_k=key).sort_values("_k").drop(columns="_k").reset_index(drop=True)


def _tint(color: str, amount: float = 0.6):
    return tuple(c + (1 - c) * amount for c in to_rgb(color))


def plot_in_vs_out(results: pd.DataFrame, colors: dict, info: dict, path: Path) -> None:
    scenarios = [s for s in results["scenario"].unique() if (results.loc[results["scenario"] == s, "ranking_basis"] != BASIS_NONE).all()]
    fig, ax = plt.subplots(figsize=(7.5, 4.8))
    width = 0.34
    for i, scenario in enumerate(scenarios):
        for j, basis in enumerate((BASIS_IN, BASIS_OUT)):
            row = results[(results["scenario"] == scenario) & (results["ranking_basis"] == basis)].iloc[0]
            value = -row["change_vs_random"] * 100
            x = i + (j - 0.5) * (width + 0.04)
            in_sample = basis == BASIS_IN
            ax.bar(x, value, width, color=_tint(colors[scenario]) if in_sample else colors[scenario],
                   edgecolor=colors[scenario], linewidth=1.2, hatch="///" if in_sample else None, zorder=2)
            if row["n_runs"] > 1:
                lo, hi = -row["change_vs_random_max"] * 100, -row["change_vs_random_min"] * 100
                ax.errorbar(x, value, yerr=[[value - lo], [hi - value]], fmt="none", ecolor=INK,
                            elinewidth=1.2, capsize=4, zorder=3)
                top = hi
            else:
                top = value
            ax.text(x, top + 0.8, f"{value:.1f}%", ha="center", va="bottom", fontsize=10, color=INK)
    ax.set_xticks(range(len(scenarios)))
    ax.set_xticklabels(scenarios, color=INK, fontsize=10)
    ax.set_ylabel("Reduction in avg. distance per order vs. random (%)", color=INK_MUTED)
    ax.set_ylim(0, max(results["change_vs_random"].abs()) * 100 * 1.25)
    ax.set_title("Slotting savings: in-sample vs. out-of-sample", color=INK, loc="left", fontsize=11)
    ax.legend(handles=[Patch(facecolor=_tint("#7a7a7a"), edgecolor="#7a7a7a", hatch="///", label=BASIS_LABEL[BASIS_IN]),
                       Patch(facecolor="#7a7a7a", edgecolor="#7a7a7a", label=BASIS_LABEL[BASIS_OUT])],
              frameon=False, fontsize=9, loc="upper right")
    ax.text(0, -0.16, f"Evaluated on {info['evaluation_orders']:,} orders on/after {SPLIT_DATE:%d %b %Y}; history = "
                      f"{info['history_orders']:,} earlier orders. Whiskers = min-max over seeds.",
            transform=ax.transAxes, fontsize=8, color=INK_MUTED)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK_MUTED, length=0)
    ax.yaxis.grid(True, color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main() -> None:
    sr = load_module("02_slotting_routing.py", "slotting_routing")
    m1 = load_module("01_order_profile_abc.py", "order_profile_abc")
    PROC.mkdir(parents=True, exist_ok=True)
    FIG.mkdir(parents=True, exist_ok=True)

    codes = sorted(pd.read_csv(PROC / "sku_velocity.csv", dtype={"StockCode": str})["StockCode"])
    history, evaluation = split_periods()
    runs, info = run_all(sr, m1.abc_class, history, evaluation, codes)

    order = [(sr.SCENARIO_RANDOM, BASIS_NONE), (sr.SCENARIO_CLASS, BASIS_IN), (sr.SCENARIO_CLASS, BASIS_OUT),
             (sr.SCENARIO_VELOCITY, BASIS_IN), (sr.SCENARIO_VELOCITY, BASIS_OUT)]
    results = summarize(runs, sr.SCENARIO_RANDOM, order)

    print(f"Split date            : {SPLIT_DATE:%Y-%m-%d}")
    print(f"History orders        : {info['history_orders']:,} (ranking period for out-of-sample)")
    print(f"Evaluation orders     : {info['evaluation_orders']:,} ({info['evaluation_visits']:,} order-SKU visits)")
    print(f"ABC class sizes       : history {info['abc_counts_history']} | evaluation {info['abc_counts_evaluation']}")
    print(f"SKUs picked in evaluation but never in history: {info['unseen_skus']:,} "
          f"({info['unseen_visit_share']:.1%} of evaluation visits)")
    print("\n--- RUNS (one row per scenario x ranking basis x seed) ---")
    print(runs.to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
    print("\n--- OUT-OF-SAMPLE RESULTS ---")
    print(results.to_string(index=False, float_format=lambda v: f"{v:,.4f}"))

    results.to_csv(PROC / "out_of_sample_results.csv", index=False)
    plot_in_vs_out(results, sr.SCENARIO_COLORS, info, FIG / "05_in_vs_out_of_sample.png")
    print(f"\nFigure -> {FIG / '05_in_vs_out_of_sample.png'}\nTable  -> {PROC / 'out_of_sample_results.csv'}")


if __name__ == "__main__":
    main()
